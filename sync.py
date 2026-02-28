#!/usr/bin/env python3

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import requests

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

base = "https://pretix.kohi.de"
with open(Path(__file__).parent / "credentials.json") as f:
    credentials = json.load(f)

auth = f"Token {credentials['pretix_token']}"

TEST_MODE = True
MAIL_FALLBACK = "mitglieder@kohi.de"


@dataclass
class Member:
    id: int
    email: str
    given_name: str
    family_name: str

    @property
    def order_code(self) -> str:
        return f"K{self.id}"

    @staticmethod
    @staticmethod
    def from_row(row: dict) -> "Member":
        data = {str(x["columnId"]): x["value"] for x in row["data"]}
        given_name = data["35"]
        family_name = data["36"]
        email = data.get("37")

        if not email:
            logger.warning(
                f"Missing email address for {given_name} {family_name} using {MAIL_FALLBACK} instead"
            )
            email = MAIL_FALLBACK
        elif not "@" in email or not "." in email or " " in email:
            logger.warning(
                f"Invalid email address for {given_name} {family_name}: '{email}' using {MAIL_FALLBACK} instead"
            )
            email = MAIL_FALLBACK

        return Member(
            id=row["id"],
            email=email,
            given_name=given_name,
            family_name=family_name,
        )

    def to_position_data(self) -> dict:
        return {
            "attendee_name_parts": {
                "given_name": self.given_name,
                "family_name": self.family_name,
            },
            "valid_from": "20260101",
            "valid_until": "29991231",
        }


@dataclass
class OrderPosition:
    id: int
    given_name: str
    family_name: str

    def from_result(res: dict) -> "OrderPosition":
        return OrderPosition(
            id=res["id"],
            given_name=res["attendee_name_parts"]["given_name"],
            family_name=res["attendee_name_parts"]["family_name"],
        )


@dataclass
class Order:
    code: str
    status: str
    positions: list[OrderPosition]

    def from_result(res: dict) -> "Order":
        return Order(
            code=res["code"],
            status=res["status"],
            positions=[OrderPosition.from_result(p) for p in res["positions"]],
        )


def get_all_order_codes() -> set[str]:
    res = requests.get(
        f"{base}/api/v1/organizers/kohi/events/mitgliedschaft/orders/?include=code",
        headers={
            "Authorization": auth,
        },
    )
    res.raise_for_status()
    return {x["code"] for x in res.json()["results"]}


def get_order(code: str) -> Order | None:
    res = requests.get(
        f"{base}/api/v1/organizers/kohi/events/mitgliedschaft/orders/{code}",
        headers={
            "Authorization": auth,
        },
    )
    if res.status_code == 404:
        return None
    else:
        res.raise_for_status()
        return Order.from_result(res.json())


def create_order(member: Member):
    logger.info("Creating orders for %s", member)
    res = requests.post(
        f"{base}/api/v1/organizers/kohi/events/mitgliedschaft/orders/",
        headers={
            "Authorization": auth,
        },
        json={
            "code": member.order_code,
            "status": "p",
            "testmode": TEST_MODE,
            "email": member.email,
            "sales_channel": "api.membership-sync",
            "positions": [
                {
                    "item": 12,
                    **member.to_position_data(),
                }
            ],
            "send_email": True,
        },
    )
    res.raise_for_status()


def update_order_position(order_position: OrderPosition, member: Member) -> None:
    logger.info("Updating order position %s with %s", order_position, member)
    res = requests.patch(
        f"{base}/api/v1/organizers/kohi/events/mitgliedschaft/orderpositions/{order_position.id}/",
        headers={
            "Authorization": auth,
        },
        json=member.to_position_data(),
    )
    res.raise_for_status()


def cancel_order(code: str) -> None:
    order = get_order(code)
    if order.status == "c":
        return
    logger.info(f"Canceling order {code}")
    res = requests.post(
        f"{base}/api/v1/organizers/kohi/events/mitgliedschaft/orders/{code}/mark_canceled/",
        headers={
            "Authorization": auth,
        },
        json={
            "comment": "Deine Mitgliedschaft wurde beendet. Falls dies nicht von dir veranlasst wurde, wende dich bitte an mitglieder@kohi.de",
            "send_email": True,
        },
    )
    res.raise_for_status()


def reactivate_order(code: str) -> None:
    logger.info(f"Reactivating order {code}")
    res = requests.post(
        f"{base}/api/v1/organizers/kohi/events/mitgliedschaft/orders/{code}/reactivate/",
        headers={
            "Authorization": auth,
        },
    )
    res.raise_for_status()


def update_order(member: Member):
    logger.info("Updating order for %s", member)
    # Email needs to be updated in order
    res = requests.patch(
        f"{base}/api/v1/organizers/kohi/events/mitgliedschaft/orders/{member.order_code}/",
        headers={
            "Authorization": auth,
        },
        json={
            "email": member.email,
        },
    )
    res.raise_for_status()
    order = Order.from_result(res.json())
    if order.status == "c":
        reactivate_order(order.code)
    # Name needs to be updated in order position
    if len(order.positions) != 1:
        raise RuntimeError(
            f"Expected exactly one order position, but got {len(order.positions)} for order {order.code}"
        )
    update_order_position(order.positions[0], member)


def create_or_update_member(member: Member) -> None:
    order = get_order(member.order_code)
    if order is None:
        create_order(member)
    else:
        update_order(member)


logging.info("Starting sync")

res = requests.get(
    url="https://cloud.kohi.de/apps/tables/api/1/views/6/rows",
    headers={"OCS-APIRequest": "true"},
    auth=(credentials["nextcloud"]["username"], credentials["nextcloud"]["password"]),
)
res.raise_for_status()
members = [Member.from_row(x) for x in res.json()]

for member in members:
    create_or_update_member(member)

order_codes = get_all_order_codes()
member_order_codes = set(m.order_code for m in members)
inactive_order_codes = order_codes.difference(member_order_codes)
for order_code in inactive_order_codes:
    cancel_order(order_code)
