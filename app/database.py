from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import ASCENDING, MongoClient

from app.config import settings


class Database:
    MANAGED_SOURCES = [
        "startup_underlying",
        "startup_option",
    ]

    def __init__(self) -> None:
        # Single client, two logical databases:
        #   * self.token_db — read-only, holds the Upstox access token
        #   * self.db       — owned by this service, holds subscriptions
        self.client = MongoClient(
            settings.mongodb_uri,
            serverSelectionTimeoutMS=5000,
        )

        self.db = self.client[settings.mongodb_db]
        self.token_db = self.client[settings.token_mongodb_db]

        self.tokens = self.token_db[settings.token_collection]

        self.active = self.db["market_subscriptions"]
        self.events = self.db["market_subscription_events"]

    def ping(self) -> None:
        self.client.admin.command("ping")

    def ensure_indexes(self) -> None:
        # Indexes only apply to the application DB.
        # The token DB is external and must not be modified.
        self.active.create_index(
            [("instrument_key", ASCENDING)],
            unique=True,
        )
        self.active.create_index(
            [("source", ASCENDING)],
        )
        self.active.create_index(
            [
                ("underlying_key", ASCENDING),
                ("expiry", ASCENDING),
                ("strike_price", ASCENDING),
                ("instrument_type", ASCENDING),
            ]
        )
        self.events.create_index(
            "expires_at",
            expireAfterSeconds=0,
        )
        self.events.create_index(
            [
                ("instrument_key", ASCENDING),
                ("created_at", ASCENDING),
            ]
        )

    def load_access_token(self) -> str:
        document = self.tokens.find_one(
            {
                "_id": settings.token_document_id,
            }
        )

        token = (document or {}).get(
            "access_token",
            "",
        ).strip()

        if not token:
            raise RuntimeError(
                "Upstox access token is missing in MongoDB"
            )

        return token

    def list_active(self) -> list[dict[str, Any]]:
        return list(
            self.active.find(
                {},
                {
                    "_id": 0,
                },
            ).sort(
                "instrument_key",
                ASCENDING,
            )
        )

    def list_managed_active(self) -> list[dict[str, Any]]:
        return list(
            self.active.find(
                {
                    "source": {
                        "$in": self.MANAGED_SOURCES,
                    }
                },
                {
                    "_id": 0,
                },
            ).sort(
                "instrument_key",
                ASCENDING,
            )
        )

    def list_manual_active(self) -> list[dict[str, Any]]:
        return list(
            self.active.find(
                {
                    "$or": [
                        {
                            "source": "manual",
                        },
                        {
                            "source": {
                                "$exists": False,
                            }
                        },
                    ]
                },
                {
                    "_id": 0,
                },
            ).sort(
                "instrument_key",
                ASCENDING,
            )
        )

    def upsert_active(
        self,
        instrument_key: str,
        mode: str,
        source: str = "manual",
        underlying_key: str | None = None,
        instrument_type: str | None = None,
        strike_price: float | None = None,
        expiry: str | None = None,
        trading_symbol: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)

        existing = self.active.find_one(
            {
                "instrument_key": instrument_key,
            }
        )

        subscription: dict[str, Any] = {
            "instrument_key": instrument_key,
            "mode": mode,
            "source": source,
            "updated_at": now,
        }

        optional_fields = {
            "underlying_key": underlying_key,
            "instrument_type": instrument_type,
            "strike_price": strike_price,
            "expiry": expiry,
            "trading_symbol": trading_symbol,
        }

        for field_name, field_value in optional_fields.items():
            if field_value is not None:
                subscription[field_name] = field_value

        self.active.update_one(
            {
                "instrument_key": instrument_key,
            },
            {
                "$set": subscription,
                "$setOnInsert": {
                    "created_at": now,
                },
            },
            upsert=True,
        )

        if existing is None:
            self.record_event(
                instrument_key=instrument_key,
                action="subscribed",
                mode=mode,
                source=source,
            )
        elif existing.get("mode") != mode:
            self.record_event(
                instrument_key=instrument_key,
                action="mode_changed",
                mode=mode,
                source=source,
            )

    def upsert_managed_subscription(
        self,
        instrument_key: str,
        mode: str,
        source: str,
        underlying_key: str | None = None,
        instrument_type: str | None = None,
        strike_price: float | None = None,
        expiry: str | None = None,
        trading_symbol: str | None = None,
    ) -> None:
        if source not in self.MANAGED_SOURCES:
            raise ValueError(
                f"Invalid managed subscription source: {source}"
            )

        now = datetime.now(timezone.utc)

        existing = self.active.find_one(
            {
                "instrument_key": instrument_key,
            }
        )

        subscription: dict[str, Any] = {
            "instrument_key": instrument_key,
            "mode": mode,
            "source": source,
            "updated_at": now,
        }

        optional_fields = {
            "underlying_key": underlying_key,
            "instrument_type": instrument_type,
            "strike_price": strike_price,
            "expiry": expiry,
            "trading_symbol": trading_symbol,
        }

        for field_name, field_value in optional_fields.items():
            if field_value is not None:
                subscription[field_name] = field_value

        unset_fields = {
            field_name: ""
            for field_name, field_value in optional_fields.items()
            if field_value is None
        }

        update_operation: dict[str, Any] = {
            "$set": subscription,
            "$setOnInsert": {
                "created_at": now,
            },
        }

        if unset_fields:
            update_operation["$unset"] = unset_fields

        self.active.update_one(
            {
                "instrument_key": instrument_key,
            },
            update_operation,
            upsert=True,
        )

        if existing is None:
            self.record_event(
                instrument_key=instrument_key,
                action="managed_subscription_added",
                mode=mode,
                source=source,
            )
        elif existing.get("mode") != mode:
            self.record_event(
                instrument_key=instrument_key,
                action="managed_mode_changed",
                mode=mode,
                source=source,
            )

    def change_mode(
        self,
        instrument_key: str,
        mode: str,
    ) -> None:
        now = datetime.now(timezone.utc)

        current = self.active.find_one(
            {
                "instrument_key": instrument_key,
            }
        )

        if current is None:
            return

        if current.get("mode") == mode:
            return

        result = self.active.update_one(
            {
                "instrument_key": instrument_key,
            },
            {
                "$set": {
                    "mode": mode,
                    "updated_at": now,
                }
            },
        )

        if result.matched_count:
            self.record_event(
                instrument_key=instrument_key,
                action="mode_changed",
                mode=mode,
                source=current.get("source"),
            )

    def remove_active(
        self,
        instrument_key: str,
    ) -> bool:
        current = self.active.find_one_and_delete(
            {
                "instrument_key": instrument_key,
            }
        )

        if not current:
            return False

        self.record_event(
            instrument_key=instrument_key,
            action="unsubscribed",
            mode=current.get("mode"),
            source=current.get("source"),
        )

        return True

    def remove_managed_not_in(
        self,
        desired_instrument_keys: set[str],
    ) -> list:
        managed_subscriptions = self.list_managed_active()

        stale_records = [
            item
            for item in managed_subscriptions
            if item.get("instrument_key")
            not in desired_instrument_keys
        ]

        stale_instrument_keys = [
            item["instrument_key"]
            for item in stale_records
            if item.get("instrument_key")
        ]

        if not stale_instrument_keys:
            return []

        self.active.delete_many(
            {
                "instrument_key": {
                    "$in": stale_instrument_keys,
                },
                "source": {
                    "$in": self.MANAGED_SOURCES,
                },
            }
        )

        for record in stale_records:
            instrument_key = record.get("instrument_key")

            if not instrument_key:
                continue

            self.record_event(
                instrument_key=instrument_key,
                action="managed_subscription_removed",
                mode=record.get("mode"),
                source=record.get("source"),
            )

        return stale_instrument_keys

    def record_event(
        self,
        instrument_key: str,
        action: str,
        mode: str | None,
        source: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)

        event: dict[str, Any] = {
            "instrument_key": instrument_key,
            "action": action,
            "mode": mode,
            "created_at": now,
            "expires_at": now + timedelta(days=7),
        }

        if source is not None:
            event["source"] = source

        self.events.insert_one(event)

    def recent_events(self) -> list[dict[str, Any]]:
        return list(
            self.events.find(
                {},
                {
                    "_id": 0,
                },
            ).sort(
                "created_at",
                -1,
            )
        )

    def close(self) -> None:
        self.client.close()