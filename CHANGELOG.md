# Changelog

All notable changes to this project will be documented in this file.

## 1.2.0 - 2026-05-19

### Added

- Added configurable `tool_response_mode` so the LLM can choose between silent handling, warning only, warning once then reporting, reporting silently, or reporting then informing the harasser.
- Added configurable `report_receiver_name` so the LLM knows who it is reporting to, such as `主人` or `狐狸`.
- Added configurable persona-aware natural-language warning replies for harassers.
- Added configurable persona-aware natural-language "already reported" replies.
- Added configurable owner report style selection between structured alerts and persona-flavored natural-language reports.
- Added optional extra LLM rewrite step for owner reports through `natural_language_report_to_owner`.
- Added `warn_message_template`, `report_inform_template`, and `warn_once_memory_seconds` config items.
- Added `.gitignore` rule for runtime-generated `data/`.

### Changed

- Updated harassment tool behavior so it no longer directly sends mechanical confirmation messages like `上报已发送给主人。` into the harasser's session.
- Changed tool-result handling to guide the LLM's in-character reply instead of plugin-side echoing.
- Improved status output and README/config documentation to explain all new switches and behavior modes.
- Updated plugin metadata to version `1.2.0` and pointed repository metadata to `Whereis-Alice/astrbot_plugin_harassment_reporter`.

### Preserved

- Kept support for recent-context summaries in reports.
- Kept support for auto-adding reported users to the watchlist.
- Kept support for cooldown, admin commands, and test reporting.

## 1.1.0 - 2026-05-19

### Added

- Initial harassment reporter plugin release.
- Added global `report_harassment` tool for LLM self-reporting.
- Added configurable report target session.
- Added optional recent-context summary attachment.
- Added optional automatic watchlist entry creation.
- Added admin commands for binding report sessions, checking status, sending test reports, and managing the watchlist.
