#!/usr/bin/env python3
"""
Caça-Voos Agente — Monitor de passagens GRU -> Itália (SerpAPI / Google Flights)
================================================================================
Roda 1x por dia e consulta um subconjunto rotativo das combinações de datas
(para caber na cota gratuita da SerpAPI, ~100 buscas/mês). Guarda histórico
de preços e notifica (e-mail e/ou WhatsApp via Evolution API) quando encontra
oferta abaixo do teto definido ou uma nova mínima histórica.

Os preços vêm do Google Flights (via SerpAPI), a fonte mais confiável
disponível para monitoramento.

Uso:
    python caca_voos.py            # execução normal
    python caca_voos.py --dry-run  # busca e mostra, mas não notifica

Configuração via variáveis de ambiente (ver .env.example).
"""

import argparse
import json
import logging
import os
import smtplib
import sys
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

SERPAPI_ENDPOINT = "https://serpapi.com/search"

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

def env(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    value = os.getenv(name, default)
    if required and not value:
        logging.error("Variável de ambiente obrigatória ausente: %s", name)
        sys.exit(1)
    return value


@dataclass
class Config:
    serpapi_key: str

    # Busca
    origin: str
    destinations: list
    departure_start: date
    departure_end: date
    stay_days: list
    currency: str
    searches_per_run: int

    # Alerta
    price_ceiling: float

    # Notificações
    smtp_host: Optional[str]
    smtp_port: int
    smtp_user: Optional[str]
    smtp_password: Optional[str]
    email_to: Optional[str]

    evolution_base_url: Optional[str]
    evolution_api_key: Optional[str]
    evolution_instance: Optional[str]
    whatsapp_number: Optional[str]

    # Persistência
    history_file: Path
    dashboard_data_file: Path
    dashboard_url: Optional[str]

    @staticmethod
    def load() -> "Config":
        # Configurações de busca vêm do docs/config.json (editável pelo
        # painel web). Variáveis de ambiente servem de fallback; segredos
        # (chaves, SMTP) continuam vindo só do ambiente.
        file_cfg = {}
        cfg_path = Path(env("CONFIG_FILE", "docs/config.json"))
        if cfg_path.exists():
            try:
                file_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                logging.info("Configurações carregadas de %s", cfg_path)
            except json.JSONDecodeError as exc:
                logging.error("config.json inválido (%s) — usando fallbacks.", exc)

        def opt(key: str, env_name: str, default):
            return file_cfg[key] if key in file_cfg else env(env_name, default)

        destinations = opt("destinations", "DESTINATIONS", "FCO,MXP,VCE")
        if isinstance(destinations, str):
            destinations = [d.strip() for d in destinations.split(",") if d.strip()]
        stay_days = opt("stay_days", "STAY_DAYS", "5,6")
        if isinstance(stay_days, str):
            stay_days = [int(x) for x in stay_days.split(",")]

        return Config(
            serpapi_key=env("SERPAPI_KEY", required=True),
            origin=str(opt("origin", "ORIGIN", "GRU")).strip().upper(),
            destinations=[str(d).upper() for d in destinations],
            departure_start=date.fromisoformat(str(opt("departure_start", "DEPARTURE_START", "2026-09-08"))),
            departure_end=date.fromisoformat(str(opt("departure_end", "DEPARTURE_END", "2026-09-12"))),
            stay_days=[int(x) for x in stay_days],
            currency=str(opt("currency", "CURRENCY", "BRL")).upper(),
            searches_per_run=int(opt("searches_per_run", "SEARCHES_PER_RUN", "3")),
            price_ceiling=float(opt("price_ceiling", "PRICE_CEILING", "4500")),
            smtp_host=env("SMTP_HOST"),
            smtp_port=int(env("SMTP_PORT", "587")),
            smtp_user=env("SMTP_USER"),
            smtp_password=env("SMTP_PASSWORD"),
            email_to=env("EMAIL_TO"),
            evolution_base_url=env("EVOLUTION_BASE_URL"),
            evolution_api_key=env("EVOLUTION_API_KEY"),
            evolution_instance=env("EVOLUTION_INSTANCE"),
            whatsapp_number=env("WHATSAPP_NUMBER"),
            history_file=Path(env("HISTORY_FILE", "price_history.json")),
            dashboard_data_file=Path(env("DASHBOARD_DATA_FILE", "docs/data.json")),
            dashboard_url=env("DASHBOARD_URL"),
        )


# ---------------------------------------------------------------------------
# Combinações de datas e rodízio
# ---------------------------------------------------------------------------

def all_combinations(cfg: Config) -> list:
    """Lista ordenada e estável de (destino, ida, volta). Os destinos são
    intercalados: com 3 buscas/dia, cada execução cobre os 3 destinos para
    um mesmo par de datas, em vez de passar dias seguidos num destino só."""
    if cfg.departure_end < cfg.departure_start:
        logging.error(
            "Config inválida: data final (%s) antes da inicial (%s). Corrija no painel.",
            cfg.departure_end, cfg.departure_start,
        )
        sys.exit(1)
    combos = []
    day = cfg.departure_start
    while day <= cfg.departure_end:
        for stay in cfg.stay_days:
            for destination in cfg.destinations:
                combos.append((destination, day, day + timedelta(days=stay)))
        day += timedelta(days=1)
    return combos


def pick_rotation(combos: list, start_index: int, count: int) -> tuple:
    """Seleciona `count` combinações a partir de start_index, com wrap-around.
    Retorna (selecionadas, próximo_índice)."""
    n = len(combos)
    selected = [combos[(start_index + i) % n] for i in range(min(count, n))]
    next_index = (start_index + count) % n
    return selected, next_index


# ---------------------------------------------------------------------------
# Cliente SerpAPI (Google Flights)
# ---------------------------------------------------------------------------

class SerpApiClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()

    def search(self, destination: str, departure: date, return_date: date) -> Optional[dict]:
        params = {
            "engine": "google_flights",
            "departure_id": self.cfg.origin,
            "arrival_id": destination,
            "outbound_date": departure.isoformat(),
            "return_date": return_date.isoformat(),
            "type": "1",           # ida e volta
            "currency": self.cfg.currency,
            "hl": "pt-br",
            "gl": "br",
            "api_key": self.cfg.serpapi_key,
        }
        for attempt in range(3):
            try:
                resp = self.session.get(SERPAPI_ENDPOINT, params=params, timeout=60)
                if resp.status_code == 429:
                    wait = 5 * (attempt + 1)
                    logging.warning("Rate limit da SerpAPI (429). Aguardando %ss...", wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                payload = resp.json()
                if "error" in payload:
                    logging.error("SerpAPI retornou erro: %s", payload["error"])
                    return None
                return payload
            except requests.RequestException as exc:
                logging.warning("Falha na consulta (tentativa %d/3): %s", attempt + 1, exc)
                time.sleep(3)
        return None


# ---------------------------------------------------------------------------
# Modelo de resultado
# ---------------------------------------------------------------------------

def fmt_minutes(total: Optional[int]) -> str:
    if not total:
        return "—"
    return f"{total // 60}h{total % 60:02d}"


@dataclass
class Offer:
    destination: str
    departure: str
    return_date: str
    price: float
    currency: str
    carrier: str
    stops_outbound: int
    duration_outbound: str
    google_flights_url: str

    @property
    def route_key(self) -> str:
        return f"{self.destination}|{self.departure}|{self.return_date}"

    def pretty(self) -> str:
        return (
            f"{self.destination}  ida {self.departure} volta {self.return_date}  "
            f"{self.currency} {self.price:,.2f}  {self.carrier}  "
            f"{self.stops_outbound} escala(s) na ida  duração ida {self.duration_outbound}"
        )


def google_flights_link(origin: str, destination: str, departure: str, return_date: str) -> str:
    """Link para abrir a mesma busca no Google Flights (para conferir/comprar)."""
    q = f"Flights from {origin} to {destination} on {departure} through {return_date}"
    return f"https://www.google.com/travel/flights?q={quote(q)}"


def parse_offers(payload: dict, cfg: Config, destination: str, departure: date, return_date: date) -> list:
    """Extrai ofertas de best_flights + other_flights do retorno da SerpAPI."""
    offers = []
    link = google_flights_link(cfg.origin, destination, departure.isoformat(), return_date.isoformat())
    for bucket in ("best_flights", "other_flights"):
        for item in payload.get(bucket, []):
            try:
                price = item.get("price")
                if price is None:
                    continue
                segments = item.get("flights", [])
                carriers = list(dict.fromkeys(s.get("airline", "?") for s in segments))
                offers.append(Offer(
                    destination=destination,
                    departure=departure.isoformat(),
                    return_date=return_date.isoformat(),
                    price=float(price),
                    currency=cfg.currency,
                    carrier=" + ".join(carriers) if carriers else "?",
                    stops_outbound=max(0, len(segments) - 1),
                    duration_outbound=fmt_minutes(item.get("total_duration")),
                    google_flights_url=link,
                ))
            except (TypeError, ValueError) as exc:
                logging.warning("Oferta ignorada (payload inesperado): %s", exc)
    return offers


# ---------------------------------------------------------------------------
# Histórico de preços (inclui o estado do rodízio)
# ---------------------------------------------------------------------------

class PriceHistory:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"routes": {}, "rotation_index": 0, "last_run": None}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
                self.data.setdefault("rotation_index", 0)
            except json.JSONDecodeError:
                logging.warning("Histórico corrompido, recomeçando do zero.")

    @property
    def rotation_index(self) -> int:
        return self.data.get("rotation_index", 0)

    @rotation_index.setter
    def rotation_index(self, value: int):
        self.data["rotation_index"] = value

    def record(self, offer: Offer) -> dict:
        route = self.data["routes"].setdefault(
            offer.route_key, {"min_price": None, "observations": []}
        )
        route["observations"].append(
            {"checked_at": datetime.now().isoformat(timespec="seconds"), "price": offer.price}
        )
        route["observations"] = route["observations"][-90:]

        previous_min = route["min_price"]
        is_new_min = previous_min is None or offer.price < previous_min
        if is_new_min:
            route["min_price"] = offer.price
        return {"is_new_min": is_new_min, "previous_min": previous_min}

    def save(self):
        self.data["last_run"] = datetime.now().isoformat(timespec="seconds")
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# Notificações
# ---------------------------------------------------------------------------

def send_email(cfg: Config, subject: str, body: str) -> bool:
    if not (cfg.smtp_host and cfg.smtp_user and cfg.smtp_password and cfg.email_to):
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = cfg.smtp_user
        msg["To"] = cfg.email_to
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(cfg.smtp_user, cfg.smtp_password)
            smtp.send_message(msg)
        logging.info("E-mail enviado para %s", cfg.email_to)
        return True
    except Exception as exc:
        logging.error("Falha ao enviar e-mail: %s", exc)
        return False


def send_whatsapp(cfg: Config, body: str) -> bool:
    if not (cfg.evolution_base_url and cfg.evolution_api_key and cfg.evolution_instance and cfg.whatsapp_number):
        return False
    try:
        resp = requests.post(
            f"{cfg.evolution_base_url.rstrip('/')}/message/sendText/{cfg.evolution_instance}",
            headers={"apikey": cfg.evolution_api_key, "Content-Type": "application/json"},
            json={"number": cfg.whatsapp_number, "text": body},
            timeout=30,
        )
        resp.raise_for_status()
        logging.info("WhatsApp enviado para %s", cfg.whatsapp_number)
        return True
    except Exception as exc:
        logging.error("Falha ao enviar WhatsApp: %s", exc)
        return False


def notify(cfg: Config, subject: str, body: str, dry_run: bool):
    if dry_run:
        logging.info("[dry-run] Notificação suprimida:\n%s\n%s", subject, body)
        return
    sent_any = send_email(cfg, subject, body) | send_whatsapp(cfg, body)
    if not sent_any:
        logging.warning("Nenhum canal de notificação configurado — resultado só no log.")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def export_dashboard_data(cfg: Config, history: PriceHistory, day_offers: list):
    """Escreve o JSON consumido pelo dashboard (GitHub Pages)."""
    cfg.dashboard_data_file.parent.mkdir(parents=True, exist_ok=True)

    # Melhor oferta conhecida de cada rota já verificada (persiste entre rodízios)
    latest_by_route = {}
    if cfg.dashboard_data_file.exists():
        try:
            previous = json.loads(cfg.dashboard_data_file.read_text(encoding="utf-8"))
            for o in previous.get("offers", []):
                latest_by_route[f"{o['destination']}|{o['departure']}|{o['return_date']}"] = o
        except json.JSONDecodeError:
            pass
    for o in day_offers:
        latest_by_route[o.route_key] = asdict(o)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "origin": cfg.origin,
        "currency": cfg.currency,
        "price_ceiling": cfg.price_ceiling,
        "destinations": cfg.destinations,
        "checked_today": [o.route_key for o in day_offers],
        "offers": sorted(latest_by_route.values(), key=lambda o: o["price"]),
        "routes": history.data["routes"],
    }
    cfg.dashboard_data_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logging.info("Dados do dashboard exportados: %s", cfg.dashboard_data_file)


# ---------------------------------------------------------------------------
# Execução principal
# ---------------------------------------------------------------------------

def run(dry_run: bool):
    cfg = Config.load()
    client = SerpApiClient(cfg)
    history = PriceHistory(cfg.history_file)

    combos = all_combinations(cfg)
    selected, next_index = pick_rotation(combos, history.rotation_index, cfg.searches_per_run)
    logging.info(
        "Rodízio: verificando %d de %d combinações (índice %d -> %d): %s",
        len(selected), len(combos), history.rotation_index, next_index,
        ", ".join(f"{d} {dep}->{ret}" for d, dep, ret in selected),
    )

    best_of_run: list = []
    alerts: list = []

    for destination, departure, return_date in selected:
        payload = client.search(destination, departure, return_date)
        if payload is None:
            continue
        offers = parse_offers(payload, cfg, destination, departure, return_date)
        if not offers:
            logging.info("Sem ofertas: %s %s -> %s", destination, departure, return_date)
            continue

        cheapest = min(offers, key=lambda o: o.price)
        best_of_run.append(cheapest)
        flags = history.record(cheapest)
        logging.info("%s%s", cheapest.pretty(), "  ** NOVA MÍNIMA **" if flags["is_new_min"] else "")

        if cheapest.price <= cfg.price_ceiling:
            alerts.append(
                f"ABAIXO DO TETO ({cfg.currency} {cfg.price_ceiling:,.2f}):\n"
                f"  {cheapest.pretty()}\n  Conferir/comprar: {cheapest.google_flights_url}"
            )
        elif flags["is_new_min"] and flags["previous_min"] is not None:
            drop = flags["previous_min"] - cheapest.price
            alerts.append(
                f"NOVA MÍNIMA (queda de {cfg.currency} {drop:,.2f}):\n"
                f"  {cheapest.pretty()}\n  Conferir/comprar: {cheapest.google_flights_url}"
            )

        time.sleep(1)

    history.rotation_index = next_index
    history.save()
    export_dashboard_data(cfg, history, best_of_run)

    if not best_of_run:
        logging.warning("Nenhuma oferta obtida nesta execução.")
        return

    overall_best = min(best_of_run, key=lambda o: o.price)
    summary_lines = [
        f"Caça-Voos — {cfg.origin} -> Itália ({date.today().isoformat()})",
        "",
        "Verificado hoje:",
    ]
    for offer in sorted(best_of_run, key=lambda o: o.price):
        summary_lines.append(f"  {offer.pretty()}")
    if cfg.dashboard_url:
        summary_lines += ["", f">> Ver todas as opções em tela: {cfg.dashboard_url}"]

    if alerts:
        body = "\n".join(["ALERTA DE PREÇO!", ""] + alerts + [""] + summary_lines)
        subject = (
            f"[Caça-Voos] ALERTA: {overall_best.currency} {overall_best.price:,.2f} "
            f"— {cfg.origin}->{overall_best.destination}"
        )
        notify(cfg, subject, body, dry_run)
    else:
        logging.info("Nenhum alerta nesta execução. Melhor do dia: %s", overall_best.pretty())
        # Descomente para receber resumo diário mesmo sem alerta:
        # notify(cfg, "[Caça-Voos] Resumo diário", "\n".join(summary_lines), dry_run)


def main():
    parser = argparse.ArgumentParser(description="Agente de monitoramento de passagens (SerpAPI/Google Flights)")
    parser.add_argument("--dry-run", action="store_true", help="Busca mas não envia notificações")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
