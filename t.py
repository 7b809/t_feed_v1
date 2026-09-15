    def _send_manual_isolation_result(
        self, chat_id, result, strike_price, option_type, requested_by
    ):
        """
        Send a concise manual instrument isolation result to the
        Telegram chat that initiated the isolation.

        The complete service result is intentionally NOT displayed
        in Telegram. Full details remain available through logging/API.
        """

        if not isinstance(result, dict):
            logger.error(
                "Manual instrument isolation returned invalid result type "
                "from Telegram. type=%s, result=%s",
                type(result).__name__,
                result,
            )
            self._send_bot_message(
                chat_id,
                "Manual instrument isolation failed.\n\n"
                "Error: Invalid service result.",
            )
            return

        success = bool(result.get("success"))

        # ============================================================
        # SUCCESS
        # ============================================================

        if success:
            isolated = result.get("isolated_instrument") or {}
            previous = result.get("previous_instrument") or {}

            isolated_contract = isolated.get("contract_info") or {}
            previous_contract = previous.get("contract_info") or {}

            isolated_instrument_key = (
                isolated.get("instrument_key")
                or isolated_contract.get("instrument_key")
                or "Not available"
            )

            isolated_trading_symbol = (
                isolated_contract.get("trading_symbol")
                or isolated.get("trading_symbol")
                or "Not available"
            )

            selected_level = (
                isolated.get("selected_level")
                or "Not available"
            )

            touch_source = (
                isolated.get("touch_source")
                or "Not available"
            )

            selected_at = (
                isolated.get("selected_at")
                or "Not available"
            )

            locked = isolated.get("locked_for_market_day")

            if locked is True:
                locked_text = "Locked for market day"
            elif locked is False:
                locked_text = "Not locked"
            else:
                locked_text = "Not available"

            # --------------------------------------------------------
            # Previous instrument
            # --------------------------------------------------------

            previous_instrument_key = (
                previous.get("instrument_key")
                or previous_contract.get("instrument_key")
            )

            previous_trading_symbol = (
                previous_contract.get("trading_symbol")
                or previous.get("trading_symbol")
            )

            previous_level = previous.get("selected_level")
            previous_touch_time = previous.get("touch_time")
            previous_touch_source = previous.get("touch_source")

            message_lines = [
                "Manual Instrument Isolation Successful",
                "",
                f"Strike: {self._format_strike_price(strike_price)} {option_type}",
                "",
                "Instrument:",
                f"• Key: {isolated_instrument_key}",
                f"• Symbol: {isolated_trading_symbol}",
                "",
                "Selection:",
                f"• Level: {selected_level}",
                f"• Source: {touch_source}",
                f"• Selected: {selected_at}",
                f"• Status: {locked_text}",
            ]

            # --------------------------------------------------------
            # Add previous instrument only when available
            # --------------------------------------------------------

            if (
                previous_instrument_key
                or previous_trading_symbol
                or previous_level
                or previous_touch_time
                or previous_touch_source
            ):
                message_lines.extend(
                    [
                        "",
                        "Previous:",
                    ]
                )

                if previous_instrument_key:
                    message_lines.append(
                        f"• Key: {previous_instrument_key}"
                    )

                if previous_trading_symbol:
                    message_lines.append(
                        f"• Symbol: {previous_trading_symbol}"
                    )

                if previous_level:
                    message_lines.append(
                        f"• Level: {previous_level}"
                    )

                if previous_touch_time:
                    message_lines.append(
                        f"• Touch: {previous_touch_time}"
                    )

                if previous_touch_source:
                    message_lines.append(
                        f"• Touch Source: {previous_touch_source}"
                    )

            message = "\n".join(message_lines)

            self._send_bot_message(chat_id, message)

            logger.info(
                "Manual instrument isolation Telegram result sent. "
                "chat_id=%s, strike=%s, option_type=%s, instrument_key=%s",
                chat_id,
                strike_price,
                option_type,
                isolated_instrument_key,
            )

            return

        # ============================================================
        # FAILURE
        # ============================================================

        error_code = str(
            result.get("error_code") or "MANUAL_ISOLATION_REJECTED"
        ).strip()

        error_message = str(
            result.get("message")
            or "Manual instrument isolation was rejected."
        ).strip()

        message = (
            "Manual Instrument Isolation Failed\n\n"
            f"Strike: {self._format_strike_price(strike_price)} {option_type}\n"
            f"Requested By: {requested_by}\n\n"
            "Failure:\n"
            f"• Code: {error_code}\n"
            f"• Message: {error_message}"
        )

        if result.get("error"):
            message += f"\n• Error: {result.get('error')}"

        self._send_bot_message(chat_id, message)

        logger.warning(
            "Manual instrument isolation rejected from Telegram. "
            "chat_id=%s, strike=%s, option_type=%s, error_code=%s",
            chat_id,
            strike_price,
            option_type,
            error_code,
        )