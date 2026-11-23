"""Constants for the Aurora Local integration."""

DOMAIN = "aurora_local"
DEFAULT_PORT = 8899
MANUFACTURER = "Adlar"
MODEL = "Castra Aurora (HF-LPT270 Wi-Fi module)"
STORE_VERSION = 1  # .storage/aurora_local.<entry_id>

CONF_RAW_LOG = "raw_log"
CONF_RETENTION_DAYS = "retention_days"
CONF_NOTIFY = "notify"
CONF_SCAN = "scan_subnet"
DEFAULT_RAW_LOG = True
DEFAULT_RETENTION_DAYS = 30
DEFAULT_NOTIFY = False
DEFAULT_SCAN = False

RAW_LOG_DIR = "aurora_local_raw"  # inside the HA config directory
RAW_FLUSH_SECONDS = 30
RECENT_RAW = 200  # messages kept in memory for diagnostics

SIGNAL_NEW_KEYS = f"{DOMAIN}_new_keys_{{}}"
SIGNAL_NEW_TABLES = f"{DOMAIN}_new_tables_{{}}"

# Bump to offer a history rebuild (repair issue) after an upgrade that changes
# how history is calculated.
# 3: v0.7.0 anchors the imported heat counter at the recorder's latest hour.
# 4: v0.8.5 shows COP at minimum compressor speed (150 W / 6 W per Hz rule).
HISTORY_VERSION = 4
HISTORY_RETRY_SECONDS = 900  # heat counter import waits for recorder statistics
HISTORY_MAX_RETRIES = 8
ISSUE_REBUILD = "rebuild_history"
ISSUE_MOVED = "module_moved"

# Name of this integration before 0.8.0 (see legacy.py).
LEGACY_DOMAIN = "adlar_castra"
LEGACY_RAW_LOG_DIR = "adlar_castra_raw"
CONF_MIGRATE_FROM = "migrate_from"
