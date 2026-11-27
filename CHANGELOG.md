# Changelog

## v1.1.0

- Added Model Workbench, a local browser interface for Ollama and compatible Chat Completions APIs.
- Choose **Stream text** or **Full response** for each message. Both modes share the conversation history for the selected provider and model.
- Compare response times and reported token usage, and export completed turns as JSON.
- Retained the original Flask and terminal examples and corrected the streaming client's completion-token parameter on the server.

## [v1.0.0] - 2024-08-14
### Added
- Introduced separate chat history directories for client and server.
- Added support for two chat options in the client: default chat and stream chat.

### Changed
- Updated README to reflect new chat options and directory structure.

### Notes
- Considered separate `requirements.txt` files for client and server dependencies.
