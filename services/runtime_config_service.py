from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import PyMongoError

from core import config
from core.logger import get_logger


logger = get_logger(__file__)

CONFIG_NAME_ISOLATION_WINDOW_POINTS = "OPENING_RANGE_ISOLATION_AVERAGE_WINDOW_POINTS"
CONFIG_NAME_ISOLATION_TOUCH_LEVELS = "OPENING_RANGE_ISOLATION_TOUCH_LEVELS"
CONFIG_NAME_ISOLATION_PRIORITY_LEVELS = "OPENING_RANGE_ISOLATION_PRIORITY_LEVELS"
CONFIG_NAME_ALLOW_BACKFILL_TOUCH = "OPENING_RANGE_ISOLATION_ALLOW_BACKFILL_TOUCH"
CONFIG_NAME_ALLOW_LIVE_TOUCH = "OPENING_RANGE_ISOLATION_ALLOW_LIVE_TOUCH"
CONFIG_NAME_ISOLATION_OPTIONS_ONLY = "OPENING_RANGE_ISOLATION_OPTIONS_ONLY"
CONFIG_NAME_ISOLATION_NOTIFY_ENABLED = "OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED"
CONFIG_NAME_LIVE_EMA_ENABLED = "LIVE_EMA_ENABLED"
CONFIG_NAME_ISOLATED_EMA_TELEGRAM_ENABLED = "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED"
CONFIG_NAME_BUDGET_RANGE_ENABLED = "EMA_ALERT_BUDGET_RANGE_ENABLED"
CONFIG_NAME_BUDGET_MIN_PRICE = "EMA_ALERT_BUDGET_MIN_PRICE"
CONFIG_NAME_BUDGET_MAX_PRICE = "EMA_ALERT_BUDGET_MAX_PRICE"
CONFIG_NAME_BUDGET_MAX_INSTRUMENTS = "EMA_ALERT_BUDGET_MAX_INSTRUMENTS"
CONFIG_NAME_NEAREST_STRIKE_COUNT = "EMA_ALERT_NEAREST_STRIKE_COUNT"
CONFIG_NAME_MAX_ORDER_INSTRUMENTS = "EMA_ALERT_MAX_ORDER_INSTRUMENTS"
CONFIG_NAME_TOUCH_ALERT_ENABLED = "OPENING_RANGE_TOUCH_ALERT_ENABLED"
CONFIG_NAME_TOUCH_ALERT_MAX_INSTRUMENTS = "OPENING_RANGE_TOUCH_ALERT_MAX_INSTRUMENTS"
CONFIG_NAME_TOUCH_ALERT_BATCH_SECONDS = "OPENING_RANGE_TOUCH_ALERT_BATCH_SECONDS"


ALLOWED_TOUCH_LEVELS = {"R1", "R2", "R3", "S1", "S2", "S3"}


def build_registry_item(label, description, category, value_type, default, minimum=None, maximum=None, allowed_values=None, suggested_values=None, restart_required=False, recalculation_required=False, telegram_editable=True):
    return {
        "label": label,
        "description": description,
        "category": category,
        "value_type": value_type,
        "default": deepcopy(default),
        "minimum": minimum,
        "maximum": maximum,
        "allowed_values": deepcopy(allowed_values or []),
        "suggested_values": deepcopy(suggested_values or []),
        "restart_required": bool(restart_required),
        "recalculation_required": bool(recalculation_required),
        "telegram_editable": bool(telegram_editable),
    }


CONFIG_REGISTRY = {
    CONFIG_NAME_ISOLATION_WINDOW_POINTS: build_registry_item(
        label="Isolation Average Window Points",
        description="Strike-selection distance above and below the main index Opening Range average.",
        category="opening_range_isolation",
        value_type="float",
        default=config.OPENING_RANGE_ISOLATION_AVERAGE_WINDOW_POINTS,
        minimum=config.OPENING_RANGE_ISOLATION_MIN_WINDOW_POINTS,
        maximum=config.OPENING_RANGE_ISOLATION_MAX_WINDOW_POINTS,
        suggested_values=[100.0, 200.0, 300.0, 500.0, 750.0, 1000.0],
        recalculation_required=True,
    ),
    CONFIG_NAME_ISOLATION_TOUCH_LEVELS: build_registry_item(
        label="Isolation Touch Levels",
        description="Opening Range levels eligible to trigger instrument isolation.",
        category="opening_range_isolation",
        value_type="string_list",
        default=config.OPENING_RANGE_ISOLATION_TOUCH_LEVELS,
        allowed_values=sorted(ALLOWED_TOUCH_LEVELS),
        suggested_values=[["R3"], ["S3"], ["R3", "S3"], ["R2", "R3", "S2", "S3"]],
    ),
    CONFIG_NAME_ISOLATION_PRIORITY_LEVELS: build_registry_item(
        label="Isolation Priority Levels",
        description="Priority order used when multiple eligible touch events exist.",
        category="opening_range_isolation",
        value_type="string_list",
        default=config.OPENING_RANGE_ISOLATION_PRIORITY_LEVELS,
        allowed_values=sorted(ALLOWED_TOUCH_LEVELS),
        suggested_values=[["R3"], ["R3", "S3"], ["R3", "S3", "R2", "S2"]],
    ),
    CONFIG_NAME_ALLOW_BACKFILL_TOUCH: build_registry_item(
        label="Allow Backfill Touch",
        description="Allows backfilled touch events to isolate an instrument.",
        category="opening_range_isolation",
        value_type="bool",
        default=config.OPENING_RANGE_ISOLATION_ALLOW_BACKFILL_TOUCH,
        suggested_values=[True, False],
    ),
    CONFIG_NAME_ALLOW_LIVE_TOUCH: build_registry_item(
        label="Allow Live Touch",
        description="Allows live touch events to isolate an instrument.",
        category="opening_range_isolation",
        value_type="bool",
        default=config.OPENING_RANGE_ISOLATION_ALLOW_LIVE_TOUCH,
        suggested_values=[True, False],
    ),
    CONFIG_NAME_ISOLATION_OPTIONS_ONLY: build_registry_item(
        label="Isolation Options Only",
        description="Restricts isolated instrument selection to option contracts.",
        category="opening_range_isolation",
        value_type="bool",
        default=config.OPENING_RANGE_ISOLATION_OPTIONS_ONLY,
        suggested_values=[True, False],
        recalculation_required=True,
    ),
    CONFIG_NAME_ISOLATION_NOTIFY_ENABLED: build_registry_item(
        label="Isolation Notification",
        description="Enables Telegram notification when an instrument is isolated.",
        category="opening_range_isolation",
        value_type="bool",
        default=config.OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED,
        suggested_values=[True, False],
    ),
    CONFIG_NAME_LIVE_EMA_ENABLED: build_registry_item(
        label="Live EMA Enabled",
        description="Enables or disables live EMA processing.",
        category="ema",
        value_type="bool",
        default=config.LIVE_EMA_ENABLED,
        suggested_values=[True, False],
    ),
    CONFIG_NAME_ISOLATED_EMA_TELEGRAM_ENABLED: build_registry_item(
        label="Isolated EMA Telegram Alerts",
        description="Enables Telegram EMA alerts for the isolated instrument.",
        category="ema",
        value_type="bool",
        default=config.EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED,
        suggested_values=[True, False],
    ),
    CONFIG_NAME_BUDGET_RANGE_ENABLED: build_registry_item(
        label="Budget Range Enabled",
        description="Enables budget-price filtering for suggested instruments.",
        category="ema_budget",
        value_type="bool",
        default=config.EMA_ALERT_BUDGET_RANGE_ENABLED,
        suggested_values=[True, False],
    ),
    CONFIG_NAME_BUDGET_MIN_PRICE: build_registry_item(
        label="Budget Minimum Price",
        description="Minimum option price for budget selection.",
        category="ema_budget",
        value_type="float",
        default=config.EMA_ALERT_BUDGET_MIN_PRICE,
        minimum=0.0,
        maximum=100000.0,
        suggested_values=[25.0, 50.0, 75.0, 100.0],
    ),
    CONFIG_NAME_BUDGET_MAX_PRICE: build_registry_item(
        label="Budget Maximum Price",
        description="Maximum option price for budget selection.",
        category="ema_budget",
        value_type="float",
        default=config.EMA_ALERT_BUDGET_MAX_PRICE,
        minimum=0.0,
        maximum=100000.0,
        suggested_values=[100.0, 120.0, 150.0, 200.0],
    ),
    CONFIG_NAME_BUDGET_MAX_INSTRUMENTS: build_registry_item(
        label="Maximum Budget Instruments",
        description="Maximum number of budget instruments included in an alert.",
        category="ema_budget",
        value_type="int",
        default=config.EMA_ALERT_BUDGET_MAX_INSTRUMENTS,
        minimum=1,
        maximum=20,
        suggested_values=[1, 2, 3, 5],
    ),
    CONFIG_NAME_NEAREST_STRIKE_COUNT: build_registry_item(
        label="Nearest Strike Count",
        description="Number of nearest strikes considered for an EMA alert.",
        category="ema_order",
        value_type="int",
        default=config.EMA_ALERT_NEAREST_STRIKE_COUNT,
        minimum=1,
        maximum=20,
        suggested_values=[1, 2, 3, 5],
    ),
    CONFIG_NAME_MAX_ORDER_INSTRUMENTS: build_registry_item(
        label="Maximum Order Instruments",
        description="Maximum number of suggested order instruments.",
        category="ema_order",
        value_type="int",
        default=config.EMA_ALERT_MAX_ORDER_INSTRUMENTS,
        minimum=1,
        maximum=20,
        suggested_values=[1, 2, 3, 5],
    ),
    CONFIG_NAME_TOUCH_ALERT_ENABLED: build_registry_item(
        label="Opening Range Touch Alerts",
        description="Enables or disables Opening Range touch alerts.",
        category="opening_range_touch",
        value_type="bool",
        default=config.OPENING_RANGE_TOUCH_ALERT_ENABLED,
        suggested_values=[True, False],
    ),
    CONFIG_NAME_TOUCH_ALERT_MAX_INSTRUMENTS: build_registry_item(
        label="Maximum Touch Alert Instruments",
        description="Maximum number of instruments shown in one touch alert.",
        category="opening_range_touch",
        value_type="int",
        default=config.OPENING_RANGE_TOUCH_ALERT_MAX_INSTRUMENTS,
        minimum=1,
        maximum=50,
        suggested_values=[1, 3, 5, 10],
    ),
    CONFIG_NAME_TOUCH_ALERT_BATCH_SECONDS: build_registry_item(
        label="Touch Alert Batch Seconds",
        description="Number of seconds used to batch touch alerts.",
        category="opening_range_touch",
        value_type="int",
        default=config.OPENING_RANGE_TOUCH_ALERT_BATCH_SECONDS,
        minimum=1,
        maximum=3600,
        suggested_values=[5, 10, 15, 30],
    ),
}


class RuntimeConfigError(Exception):
    pass


class RuntimeConfigValidationError(RuntimeConfigError):
    pass


class RuntimeConfigNotFoundError(RuntimeConfigError):
    pass


class RuntimeConfigNotEditableError(RuntimeConfigError):
    pass


class RuntimeConfigPersistenceError(RuntimeConfigError):
    pass


class RuntimeConfigService:
    def __init__(self):
        self._lock = RLock()
        self._client = None
        self._database = None
        self._config_collection = None
        self._audit_collection = None
        self._cache = {}
        self._initialized = False
        self._initialization_error = None

    @property
    def enabled(self):
        return bool(config.RUNTIME_CONFIG_ENABLED)

    @property
    def initialized(self):
        with self._lock:
            return self._initialized

    def initialize(self):
        with self._lock:
            if self._initialized:
                return self.get_status()
            if not self.enabled:
                self._initialized = True
                logger.info("Runtime configuration service is disabled.")
                return self.get_status()
            try:
                self._connect()
                self._create_indexes()
                self._load_cache()
                self._initialized = True
                self._initialization_error = None
                logger.info("Runtime configuration service initialized with %s override(s).", len(self._cache))
            except Exception as exc:
                logger.exception("Runtime configuration initialization failed: %s", exc)
                self._initialization_error = str(exc)
                if not config.RUNTIME_CONFIG_FAIL_OPEN:
                    raise RuntimeConfigPersistenceError("Runtime configuration initialization failed.") from exc
                self._initialized = True
                self._cache = {}
            return self.get_status()

    def close(self):
        with self._lock:
            if self._client is not None:
                self._client.close()
            self._client = None
            self._database = None
            self._config_collection = None
            self._audit_collection = None
            self._cache = {}
            self._initialized = False
            self._initialization_error = None

    def get_status(self):
        with self._lock:
            return {
                "enabled": self.enabled,
                "initialized": self._initialized,
                "database_connected": self._config_collection is not None,
                "cache_enabled": bool(config.RUNTIME_CONFIG_CACHE_ENABLED),
                "override_count": len(self._cache),
                "registry_count": len(CONFIG_REGISTRY),
                "error": self._initialization_error,
            }

    def get_categories(self, telegram_only=False):
        categories = set()
        for metadata in CONFIG_REGISTRY.values():
            if telegram_only and not metadata.get("telegram_editable", False):
                continue
            category = str(metadata.get("category") or "").strip()
            if category:
                categories.add(category)
        return sorted(categories)

    def get_registry(self, telegram_only=False, category=None):
        normalized_category = str(category or "").strip().lower()
        result = {}
        for name, metadata in CONFIG_REGISTRY.items():
            if telegram_only and not metadata.get("telegram_editable", False):
                continue
            item_category = str(metadata.get("category") or "").strip().lower()
            if normalized_category and item_category != normalized_category:
                continue
            item = deepcopy(metadata)
            item["name"] = name
            item["effective_value"] = self.get(name)
            item["has_runtime_override"] = self.has_runtime_override(name)
            result[name] = item
        return result

    def get_metadata(self, name):
        normalized_name = self._normalize_name(name)
        metadata = self._get_registered_metadata(normalized_name)
        result = deepcopy(metadata)
        result["name"] = normalized_name
        result["effective_value"] = self.get(normalized_name)
        result["has_runtime_override"] = self.has_runtime_override(normalized_name)
        return result

    def get(self, name, default=None):
        normalized_name = self._normalize_name(name)
        metadata = CONFIG_REGISTRY.get(normalized_name)
        if metadata is None:
            if default is not None:
                return deepcopy(default)
            raise RuntimeConfigNotFoundError(f"Runtime configuration is not registered: {normalized_name}")
        if not self.enabled:
            return deepcopy(metadata["default"])
        self._ensure_initialized()
        if config.RUNTIME_CONFIG_CACHE_ENABLED:
            with self._lock:
                if normalized_name in self._cache:
                    return deepcopy(self._cache[normalized_name])
            return deepcopy(metadata["default"])
        database_value = self._read_database_value(normalized_name)
        if database_value is None:
            return deepcopy(metadata["default"])
        return deepcopy(database_value)

    def get_float(self, name, default=None):
        value = self.get(name, default)
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeConfigValidationError(f"{name} does not contain a valid float value.") from exc

    def get_int(self, name, default=None):
        value = self.get(name, default)
        if isinstance(value, bool):
            raise RuntimeConfigValidationError(f"{name} does not contain a valid integer value.")
        try:
            numeric_value = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeConfigValidationError(f"{name} does not contain a valid integer value.") from exc
        if not numeric_value.is_integer():
            raise RuntimeConfigValidationError(f"{name} must contain a whole-number integer.")
        return int(numeric_value)

    def get_bool(self, name, default=None):
        value = self.get(name, default)
        return self._parse_bool(value)

    def get_string_list(self, name, default=None):
        value = self.get(name, default or [])
        return self._parse_string_list(value)

    def get_all_effective_values(self, category=None, telegram_only=False):
        registry = self.get_registry(telegram_only=telegram_only, category=category)
        return {name: deepcopy(item["effective_value"]) for name, item in registry.items()}

    def set(self, name, value, changed_by, source="application", chat_id=None, reason=None):
        normalized_name = self._normalize_name(name)
        metadata = self._get_registered_metadata(normalized_name)
        normalized_source = str(source or "application").strip().lower() or "application"
        if normalized_source == "telegram" and not metadata.get("telegram_editable", False):
            raise RuntimeConfigNotEditableError(f"{normalized_name} cannot be edited from Telegram.")
        validated_value = self.validate(normalized_name, value)
        self._validate_related_values(normalized_name, validated_value)
        self._ensure_initialized()
        old_value = self.get(normalized_name)
        changed = old_value != validated_value
        changed_at = self._utc_now()
        normalized_changed_by = str(changed_by or "unknown").strip() or "unknown"
        normalized_chat_id = str(chat_id) if chat_id is not None else None
        normalized_reason = str(reason).strip() if reason is not None else None
        document = {
            "_id": normalized_name,
            "name": normalized_name,
            "value": deepcopy(validated_value),
            "value_type": metadata["value_type"],
            "category": metadata["category"],
            "updated_at": changed_at,
            "updated_by": normalized_changed_by,
            "source": normalized_source,
            "chat_id": normalized_chat_id,
            "reason": normalized_reason,
            "restart_required": bool(metadata.get("restart_required", False)),
            "recalculation_required": bool(metadata.get("recalculation_required", False)),
        }
        persisted = False
        if self.enabled and self._config_collection is not None:
            try:
                self._config_collection.update_one(
                    {"_id": normalized_name},
                    {"$set": document, "$setOnInsert": {"created_at": changed_at}},
                    upsert=True,
                )
                persisted = True
            except PyMongoError as exc:
                logger.exception("Failed to persist runtime configuration %s.", normalized_name)
                if not config.RUNTIME_CONFIG_FAIL_OPEN:
                    raise RuntimeConfigPersistenceError(f"Unable to save {normalized_name}.") from exc
        with self._lock:
            self._cache[normalized_name] = deepcopy(validated_value)
        if changed:
            self._write_audit_record(
                action="set", name=normalized_name, old_value=old_value, new_value=validated_value,
                changed_by=normalized_changed_by, source=normalized_source, chat_id=normalized_chat_id, reason=normalized_reason
            )
        logger.info(
            "Runtime configuration processed: name=%s, old_value=%s, new_value=%s, changed=%s, persisted=%s, source=%s, changed_by=%s",
            normalized_name, old_value, validated_value, changed, persisted, normalized_source, normalized_changed_by
        )
        return {
            "success": True,
            "changed": changed,
            "name": normalized_name,
            "label": metadata["label"],
            "old_value": deepcopy(old_value),
            "new_value": deepcopy(validated_value),
            "effective_value": self.get(normalized_name),
            "persisted": persisted,
            "updated_at": changed_at,
            "updated_by": normalized_changed_by,
            "source": normalized_source,
            "chat_id": normalized_chat_id,
            "restart_required": bool(metadata.get("restart_required", False)),
            "recalculation_required": bool(metadata.get("recalculation_required", False)),
        }

    def reset(self, name, changed_by, source="application", chat_id=None, reason=None):
        normalized_name = self._normalize_name(name)
        metadata = self._get_registered_metadata(normalized_name)
        normalized_source = str(source or "application").strip().lower() or "application"
        if normalized_source == "telegram" and not metadata.get("telegram_editable", False):
            raise RuntimeConfigNotEditableError(f"{normalized_name} cannot be reset from Telegram.")
        self._ensure_initialized()
        old_value = self.get(normalized_name)
        default_value = deepcopy(metadata["default"])
        normalized_changed_by = str(changed_by or "unknown").strip() or "unknown"
        normalized_chat_id = str(chat_id) if chat_id is not None else None
        normalized_reason = str(reason).strip() if reason is not None else None
        deleted = False
        if self.enabled and self._config_collection is not None:
            try:
                delete_result = self._config_collection.delete_one({"_id": normalized_name})
                deleted = delete_result.deleted_count > 0
            except PyMongoError as exc:
                logger.exception("Failed to reset runtime configuration %s.", normalized_name)
                if not config.RUNTIME_CONFIG_FAIL_OPEN:
                    raise RuntimeConfigPersistenceError(f"Unable to reset {normalized_name}.") from exc
        with self._lock:
            self._cache.pop(normalized_name, None)
        changed = old_value != default_value
        if changed or deleted:
            self._write_audit_record(
                action="reset", name=normalized_name, old_value=old_value, new_value=default_value,
                changed_by=normalized_changed_by, source=normalized_source, chat_id=normalized_chat_id, reason=normalized_reason
            )
        logger.info("Runtime configuration reset: name=%s, old_value=%s, default_value=%s, deleted=%s", normalized_name, old_value, default_value, deleted)
        return {
            "success": True,
            "changed": changed,
            "name": normalized_name,
            "label": metadata["label"],
            "old_value": deepcopy(old_value),
            "new_value": deepcopy(default_value),
            "effective_value": deepcopy(default_value),
            "override_deleted": deleted,
            "updated_at": self._utc_now(),
            "updated_by": normalized_changed_by,
            "source": normalized_source,
            "chat_id": normalized_chat_id,
            "restart_required": bool(metadata.get("restart_required", False)),
            "recalculation_required": bool(metadata.get("recalculation_required", False)),
        }

    def reset_all(self, changed_by, source="application", chat_id=None, reason=None):
        results = []
        failures = []
        for name in CONFIG_REGISTRY:
            try:
                reset_result = self.reset(name=name, changed_by=changed_by, source=source, chat_id=chat_id, reason=reason)
                results.append(reset_result)
            except RuntimeConfigError as exc:
                failures.append({"name": name, "error": str(exc)})
        return {"success": not failures, "reset_count": len(results), "failure_count": len(failures), "results": results, "failures": failures}

    def validate(self, name, value):
        normalized_name = self._normalize_name(name)
        metadata = self._get_registered_metadata(normalized_name)
        value_type = metadata["value_type"]
        parsers = {"bool": self._parse_bool, "int": self._parse_int, "float": self._parse_float, "string": self._parse_string, "string_list": self._parse_string_list, "int_list": self._parse_int_list}
        parser = parsers.get(value_type)
        if parser is None:
            raise RuntimeConfigValidationError(f"Unsupported value type for {normalized_name}: {value_type}")
        parsed_value = parser(value)
        self._validate_limits(normalized_name, parsed_value, metadata)
        self._validate_allowed_values(normalized_name, parsed_value, metadata)
        return parsed_value

    def has_runtime_override(self, name):
        normalized_name = self._normalize_name(name)
        if normalized_name not in CONFIG_REGISTRY:
            return False
        self._ensure_initialized()
        if config.RUNTIME_CONFIG_CACHE_ENABLED:
            with self._lock:
                return normalized_name in self._cache
        if self._config_collection is None:
            return False
        try:
            count = self._config_collection.count_documents({"_id": normalized_name}, limit=1)
            return count > 0
        except PyMongoError:
            logger.exception("Failed to check runtime override for %s.", normalized_name)
            return False

    def refresh(self):
        self._ensure_initialized()
        if self.enabled and self._config_collection is not None:
            self._load_cache()
        return self.get_status()

    def get_audit_history(self, name=None, limit=50):
        self._ensure_initialized()
        if self._audit_collection is None:
            return []
        safe_limit = max(1, min(int(limit), 500))
        query = {}
        if name:
            normalized_name = self._normalize_name(name)
            self._get_registered_metadata(normalized_name)
            query["name"] = normalized_name
        try:
            cursor = self._audit_collection.find(query, {"_id": 0}).sort("changed_at", DESCENDING).limit(safe_limit)
            return list(cursor)
        except PyMongoError as exc:
            logger.exception("Failed to read runtime configuration audit history.")
            if config.RUNTIME_CONFIG_FAIL_OPEN:
                return []
            raise RuntimeConfigPersistenceError("Unable to read runtime configuration audit history.") from exc

    def _connect(self):
        if not config.MONGO_URI:
            raise RuntimeConfigPersistenceError("MONGO_URL is not configured.")
        if not config.MONGO_DB:
            raise RuntimeConfigPersistenceError("MONGO_DB is not configured.")
        self._client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, socketTimeoutMS=10000)
        self._client.admin.command("ping")
        self._database = self._client[config.MONGO_DB]
        self._config_collection = self._database[config.RUNTIME_CONFIG_COLLECTION]
        self._audit_collection = self._database[config.RUNTIME_CONFIG_AUDIT_COLLECTION]

    def _create_indexes(self):
        if self._config_collection is not None:
            self._config_collection.create_index([("category", ASCENDING)], name="runtime_config_category_idx")
            self._config_collection.create_index([("updated_at", DESCENDING)], name="runtime_config_updated_at_idx")
        if self._audit_collection is not None:
            self._audit_collection.create_index([("name", ASCENDING), ("changed_at", DESCENDING)], name="runtime_config_audit_name_time_idx")
            self._audit_collection.create_index([("changed_at", DESCENDING)], name="runtime_config_audit_time_idx")

    def _load_cache(self):
        if self._config_collection is None:
            with self._lock:
                self._cache = {}
            return
        try:
            documents = list(self._config_collection.find({}))
        except PyMongoError as exc:
            raise RuntimeConfigPersistenceError("Unable to load runtime configurations.") from exc
        loaded_values = {}
        for document in documents:
            name = self._normalize_name(document.get("name") or document.get("_id"))
            if name not in CONFIG_REGISTRY:
                logger.warning("Ignoring unregistered runtime configuration: %s", name)
                continue
            try:
                loaded_values[name] = self.validate(name, document.get("value"))
            except RuntimeConfigValidationError as exc:
                logger.warning("Ignoring invalid runtime configuration %s: %s", name, exc)
        with self._lock:
            self._cache = loaded_values

    def _read_database_value(self, name):
        if self._config_collection is None:
            return None
        try:
            document = self._config_collection.find_one({"_id": name})
        except PyMongoError as exc:
            logger.exception("Failed to read runtime configuration %s.", name)
            if config.RUNTIME_CONFIG_FAIL_OPEN:
                return None
            raise RuntimeConfigPersistenceError(f"Unable to read {name}.") from exc
        if not document:
            return None
        try:
            return self.validate(name, document.get("value"))
        except RuntimeConfigValidationError:
            logger.exception("Stored runtime value is invalid for %s.", name)
            return None

    def _write_audit_record(self, action, name, old_value, new_value, changed_by, source, chat_id, reason):
        if not config.RUNTIME_CONFIG_AUDIT_ENABLED:
            return
        if self._audit_collection is None:
            return
        metadata = CONFIG_REGISTRY.get(name, {})
        document = {
            "action": action,
            "name": name,
            "category": metadata.get("category"),
            "old_value": deepcopy(old_value),
            "new_value": deepcopy(new_value),
            "changed_at": self._utc_now(),
            "changed_by": changed_by,
            "source": source,
            "chat_id": chat_id,
            "reason": reason,
        }
        try:
            self._audit_collection.insert_one(document)
            self._trim_audit_collection()
        except PyMongoError:
            logger.exception("Failed to write runtime configuration audit record for %s.", name)

    def _trim_audit_collection(self):
        if self._audit_collection is None:
            return
        maximum_records = max(1, int(config.RUNTIME_CONFIG_MAX_AUDIT_RECORDS))
        try:
            total_records = self._audit_collection.estimated_document_count()
            excess_records = total_records - maximum_records
            if excess_records <= 0:
                return
            oldest_documents = list(self._audit_collection.find({}, {"_id": 1}).sort("changed_at", ASCENDING).limit(excess_records))
            identifiers = [doc["_id"] for doc in oldest_documents if "_id" in doc]
            if identifiers:
                self._audit_collection.delete_many({"_id": {"$in": identifiers}})
        except PyMongoError:
            logger.exception("Failed to trim runtime configuration audit records.")

    def _validate_related_values(self, name, value):
        if name == CONFIG_NAME_BUDGET_MIN_PRICE:
            maximum_price = self.get(CONFIG_NAME_BUDGET_MAX_PRICE)
            if float(value) > float(maximum_price):
                raise RuntimeConfigValidationError("EMA_ALERT_BUDGET_MIN_PRICE cannot be greater than EMA_ALERT_BUDGET_MAX_PRICE.")
        if name == CONFIG_NAME_BUDGET_MAX_PRICE:
            minimum_price = self.get(CONFIG_NAME_BUDGET_MIN_PRICE)
            if float(value) < float(minimum_price):
                raise RuntimeConfigValidationError("EMA_ALERT_BUDGET_MAX_PRICE cannot be less than EMA_ALERT_BUDGET_MIN_PRICE.")

    @staticmethod
    def _validate_limits(name, value, metadata):
        minimum = metadata.get("minimum")
        maximum = metadata.get("maximum")
        if minimum is not None and value < minimum:
            raise RuntimeConfigValidationError(f"{name} must be greater than or equal to {minimum}.")
        if maximum is not None and value > maximum:
            raise RuntimeConfigValidationError(f"{name} must be less than or equal to {maximum}.")

    @staticmethod
    def _validate_allowed_values(name, value, metadata):
        allowed_values = metadata.get("allowed_values")
        if not allowed_values:
            return
        allowed_set = {str(item).strip().upper() for item in allowed_values}
        if isinstance(value, list):
            values_to_check = value
        else:
            values_to_check = [value]
        invalid_values = [item for item in values_to_check if str(item).strip().upper() not in allowed_set]
        if invalid_values:
            raise RuntimeConfigValidationError(f"{name} contains unsupported values: {invalid_values}. Allowed values: {sorted(allowed_set)}.")

    def _ensure_initialized(self):
        if not self.initialized:
            self.initialize()

    @staticmethod
    def _get_registered_metadata(name):
        metadata = CONFIG_REGISTRY.get(name)
        if metadata is None:
            raise RuntimeConfigNotFoundError(f"Runtime configuration is not registered: {name}")
        return metadata

    @staticmethod
    def _normalize_name(name):
        return str(name or "").strip().upper()

    @staticmethod
    def _parse_bool(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        normalized_value = str(value or "").strip().lower()
        true_values = {"true", "1", "yes", "y", "on", "enable", "enabled"}
        false_values = {"false", "0", "no", "n", "off", "disable", "disabled"}
        if normalized_value in true_values:
            return True
        if normalized_value in false_values:
            return False
        raise RuntimeConfigValidationError("Expected a boolean value such as true or false.")

    @staticmethod
    def _parse_int(value):
        if isinstance(value, bool):
            raise RuntimeConfigValidationError("Boolean value cannot be used as an integer.")
        try:
            numeric_value = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeConfigValidationError("Expected a valid integer value.") from exc
        if not numeric_value.is_integer():
            raise RuntimeConfigValidationError("Expected a whole-number integer value.")
        return int(numeric_value)

    @staticmethod
    def _parse_float(value):
        if isinstance(value, bool):
            raise RuntimeConfigValidationError("Boolean value cannot be used as a number.")
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeConfigValidationError("Expected a valid numeric value.") from exc

    @staticmethod
    def _parse_string(value):
        parsed_value = str(value or "").strip()
        if not parsed_value:
            raise RuntimeConfigValidationError("String value cannot be empty.")
        return parsed_value

    @staticmethod
    def _parse_string_list(value):
        if isinstance(value, str):
            raw_values = value.split(",")
        elif isinstance(value, (list, tuple, set)):
            raw_values = list(value)
        else:
            raise RuntimeConfigValidationError("Expected a comma-separated string or a list.")
        parsed_values = []
        seen_values = set()
        for item in raw_values:
            parsed_item = str(item or "").strip().upper()
            if not parsed_item:
                continue
            if parsed_item in seen_values:
                continue
            seen_values.add(parsed_item)
            parsed_values.append(parsed_item)
        if not parsed_values:
            raise RuntimeConfigValidationError("At least one list value is required.")
        return parsed_values

    @staticmethod
    def _parse_int_list(value):
        if isinstance(value, str):
            raw_values = value.split(",")
        elif isinstance(value, (list, tuple, set)):
            raw_values = list(value)
        else:
            raise RuntimeConfigValidationError("Expected a comma-separated string or a list.")
        parsed_values = []
        for item in raw_values:
            if not str(item).strip():
                continue
            parsed_values.append(RuntimeConfigService._parse_int(item))
        if not parsed_values:
            raise RuntimeConfigValidationError("At least one integer list value is required.")
        return parsed_values

    @staticmethod
    def _utc_now():
        return datetime.now(timezone.utc)


runtime_config_service = RuntimeConfigService()


def initialize_runtime_config():
    return runtime_config_service.initialize()


def close_runtime_config():
    runtime_config_service.close()


def refresh_runtime_config():
    return runtime_config_service.refresh()


def get_runtime_config(name, default=None):
    return runtime_config_service.get(name=name, default=default)


def get_runtime_float(name, default=None):
    return runtime_config_service.get_float(name=name, default=default)


def get_runtime_int(name, default=None):
    return runtime_config_service.get_int(name=name, default=default)


def get_runtime_bool(name, default=None):
    return runtime_config_service.get_bool(name=name, default=default)


def get_runtime_string_list(name, default=None):
    return runtime_config_service.get_string_list(name=name, default=default)


def set_runtime_config(name, value, changed_by, source="application", chat_id=None, reason=None):
    return runtime_config_service.set(name=name, value=value, changed_by=changed_by, source=source, chat_id=chat_id, reason=reason)


def reset_runtime_config(name, changed_by, source="application", chat_id=None, reason=None):
    return runtime_config_service.reset(name=name, changed_by=changed_by, source=source, chat_id=chat_id, reason=reason)


def reset_all_runtime_configs(changed_by, source="application", chat_id=None, reason=None):
    return runtime_config_service.reset_all(changed_by=changed_by, source=source, chat_id=chat_id, reason=reason)


def get_runtime_config_registry(telegram_only=False, category=None):
    return runtime_config_service.get_registry(telegram_only=telegram_only, category=category)


def get_runtime_config_categories(telegram_only=False):
    return runtime_config_service.get_categories(telegram_only=telegram_only)


def get_runtime_config_metadata(name):
    return runtime_config_service.get_metadata(name)


def get_runtime_config_audit_history(name=None, limit=50):
    return runtime_config_service.get_audit_history(name=name, limit=limit)


def get_isolation_window_points():
    return get_runtime_float(CONFIG_NAME_ISOLATION_WINDOW_POINTS, config.OPENING_RANGE_ISOLATION_AVERAGE_WINDOW_POINTS)


def get_isolation_touch_levels():
    return get_runtime_string_list(CONFIG_NAME_ISOLATION_TOUCH_LEVELS, config.OPENING_RANGE_ISOLATION_TOUCH_LEVELS)


def get_isolation_priority_levels():
    return get_runtime_string_list(CONFIG_NAME_ISOLATION_PRIORITY_LEVELS, config.OPENING_RANGE_ISOLATION_PRIORITY_LEVELS)


def get_allow_backfill_touch():
    return get_runtime_bool(CONFIG_NAME_ALLOW_BACKFILL_TOUCH, config.OPENING_RANGE_ISOLATION_ALLOW_BACKFILL_TOUCH)


def get_allow_live_touch():
    return get_runtime_bool(CONFIG_NAME_ALLOW_LIVE_TOUCH, config.OPENING_RANGE_ISOLATION_ALLOW_LIVE_TOUCH)


def get_isolation_options_only():
    return get_runtime_bool(CONFIG_NAME_ISOLATION_OPTIONS_ONLY, config.OPENING_RANGE_ISOLATION_OPTIONS_ONLY)


def get_isolation_notify_enabled():
    return get_runtime_bool(CONFIG_NAME_ISOLATION_NOTIFY_ENABLED, config.OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED)


def get_live_ema_enabled():
    return get_runtime_bool(CONFIG_NAME_LIVE_EMA_ENABLED, config.LIVE_EMA_ENABLED)


def get_isolated_ema_telegram_enabled():
    return get_runtime_bool(CONFIG_NAME_ISOLATED_EMA_TELEGRAM_ENABLED, config.EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED)


def get_budget_range_enabled():
    return get_runtime_bool(CONFIG_NAME_BUDGET_RANGE_ENABLED, config.EMA_ALERT_BUDGET_RANGE_ENABLED)


def get_budget_min_price():
    return get_runtime_float(CONFIG_NAME_BUDGET_MIN_PRICE, config.EMA_ALERT_BUDGET_MIN_PRICE)


def get_budget_max_price():
    return get_runtime_float(CONFIG_NAME_BUDGET_MAX_PRICE, config.EMA_ALERT_BUDGET_MAX_PRICE)


def get_budget_max_instruments():
    return get_runtime_int(CONFIG_NAME_BUDGET_MAX_INSTRUMENTS, config.EMA_ALERT_BUDGET_MAX_INSTRUMENTS)


def get_nearest_strike_count():
    return get_runtime_int(CONFIG_NAME_NEAREST_STRIKE_COUNT, config.EMA_ALERT_NEAREST_STRIKE_COUNT)


def get_max_order_instruments():
    return get_runtime_int(CONFIG_NAME_MAX_ORDER_INSTRUMENTS, config.EMA_ALERT_MAX_ORDER_INSTRUMENTS)


def get_touch_alert_enabled():
    return get_runtime_bool(CONFIG_NAME_TOUCH_ALERT_ENABLED, config.OPENING_RANGE_TOUCH_ALERT_ENABLED)


def get_touch_alert_max_instruments():
    return get_runtime_int(CONFIG_NAME_TOUCH_ALERT_MAX_INSTRUMENTS, config.OPENING_RANGE_TOUCH_ALERT_MAX_INSTRUMENTS)


def get_touch_alert_batch_seconds():
    return get_runtime_int(CONFIG_NAME_TOUCH_ALERT_BATCH_SECONDS, config.OPENING_RANGE_TOUCH_ALERT_BATCH_SECONDS)