import logging
import re
from typing import Dict, Any, List, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.config import settings
from app.upstox_options.fetch_options import options_cache
from app.services.history_service import HistoryService

logger = logging.getLogger("uvicorn")

# In-memory storage for option historical candle and EMA data
options_history_cache: Dict[str, Dict[str, Any]] = {}


class OptionsBatchHistoryService:
    def __init__(self, max_workers: int = 5):
        """
        :param max_workers: Number of concurrent threads for fetching options history.
        """
        self.history_service = HistoryService()
        self.max_workers = max_workers

    @staticmethod
    def _sanitize_folder_name(trading_symbol: str) -> str:
        """
        Sanitizes trading symbols for safe local directory creation.
        Example: 'NSE_FO|63935' or 'NIFTY24JUL24500CE' -> safe folder path format.
        """
        return re.sub(r'[\\/*?:"<>|]', "_", trading_symbol).replace(" ", "_")

    def _fetch_single_contract_history(
        self, contract: Dict[str, Any], save_files: bool
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """
        Worker function executed inside a thread pool.
        Fetches history for a single contract, saves candle files locally, and returns data.
        """
        instrument_key = contract.get("instrument_key")
        trading_symbol = contract.get("trading_symbol") or instrument_key
        strike = contract.get("strike_price")
        itype = contract.get("instrument_type")

        if not instrument_key:
            logger.error(f"Missing instrument key for contract: {trading_symbol}")
            return None, None

        safe_folder = self._sanitize_folder_name(trading_symbol)
        output_dir = f"data/options_history/{safe_folder}"

        try:
            # Load historical 7 days + intraday candles (merged & deduplicated)
            result = self.history_service.load_candles(
                instrument_key=instrument_key,
                include_intraday=True,
                save_files=save_files,
                output_dir=output_dir,
            )

            if not result or result.get("total_candles", 0) == 0:
                logger.warning(
                    f"No candles retrieved for contract {trading_symbol} ({instrument_key})"
                )
                return None, None

            data_dict = {
                "trading_symbol": trading_symbol,
                "strike_price": strike,
                "instrument_type": itype,
                "expiry": contract.get("expiry"),
                "history_days": result.get("history_days"),
                "total_candles": result.get("total_candles"),
                "total_crossovers": result.get("total_crossovers"),
                "ema": result.get("ema"),
            }

            return instrument_key, data_dict

        except Exception as e:
            logger.error(
                f"Failed to fetch history for {trading_symbol} ({instrument_key}): {e}"
            )
            return None, None

    def process_target_options_history(
        self,
        min_strike: Optional[float] = None,
        max_strike: Optional[float] = None,
        save_files: Optional[bool] = None,
        batch_log_size: int = 10,
    ) -> Dict[str, Any]:
        """
        Loops through options in options_cache, merges historical + intraday candles,
        computes EMAs, and writes output files locally for every contract concurrently.
        """
        global options_history_cache

        # Fallback to configured settings or reasonable default boundaries
        if min_strike is None:
            min_strike = float(getattr(settings, "STRIKE_FROM", 23000.0))
        if max_strike is None:
            max_strike = float(getattr(settings, "STRIKE_TO", 25000.0))
        if save_files is None:
            save_files = getattr(settings, "SAVE_OPTIONS_DATA", True)

        data = options_cache.get("data", [])
        if not data:
            logger.warning("Options cache is empty! Cannot run historical cross-check.")
            return {"status": "error", "message": "Options cache is empty."}

        # Filter contracts within min_strike to max_strike range for both CE and PE
        target_contracts: List[Dict[str, Any]] = [
            item
            for item in data
            if item.get("strike_price") is not None
            and min_strike <= float(item["strike_price"]) <= max_strike
        ]

        total_targets = len(target_contracts)
        logger.info(
            f"Starting historical processing for {total_targets} option contracts "
            f"(Strikes {min_strike} to {max_strike}) | save_files={save_files} | workers={self.max_workers}"
        )

        processed_count = 0
        completed_tasks = 0

        # Execute concurrent batch requests
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_symbol = {
                executor.submit(
                    self._fetch_single_contract_history, contract, save_files
                ): contract.get("trading_symbol", contract.get("instrument_key"))
                for contract in target_contracts
            }

            for future in as_completed(future_to_symbol):
                symbol = future_to_symbol[future]
                completed_tasks += 1

                try:
                    instrument_key, history_data = future.result()
                    if instrument_key and history_data:
                        options_history_cache[instrument_key] = history_data
                        processed_count += 1
                except Exception as e:
                    logger.error(f"Execution error loading history for {symbol}: {e}")

                if (
                    completed_tasks % batch_log_size == 0
                    or completed_tasks == total_targets
                ):
                    logger.info(
                        f"Progress: Processed {completed_tasks}/{total_targets} option contracts..."
                    )

        logger.info(
            f"Historical batch execution complete! Successfully saved and cached {processed_count}/{total_targets} contracts."
        )

        return {
            "status": "success",
            "processed_contracts": processed_count,
            "total_target_contracts": total_targets,
            "nearest_expiry": options_cache.get("nearest_expiry"),
        }


batch_history_service = OptionsBatchHistoryService(max_workers=5)
