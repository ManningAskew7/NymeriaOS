//! Source-checkout auto-configuration: reads the bootstrap admin token from
//! `<runtime_root>/data/BOOTSTRAP_TOKEN.txt` (created by the agent on first
//! launch against an empty users table), and ensures a minimal env file exists.
//! Installed beta builds are client-only and do not call this path.
//!
//! The legacy `NYMERIA_API_KEY` shared key was retired in Step 3c — the
//! backend only accepts per-user `nym_...` tokens now, so we no longer
//! generate one.

use std::fs;
use std::path::{Path, PathBuf};

fn env_path_for_runtime_root(runtime_root: &Path) -> PathBuf {
    if runtime_root.join("run.py").exists() {
        runtime_root.join(".env")
    } else {
        runtime_root.join("config.env")
    }
}

/// Ensure the env file exists, then return the bootstrap admin token from
/// `data/BOOTSTRAP_TOKEN.txt` if present (empty string otherwise — the user is
/// expected to paste a token via the Setup Wizard).
pub fn ensure_env_file(runtime_root: &Path) -> Result<String, String> {
    let env_path = env_path_for_runtime_root(runtime_root);

    if !env_path.exists() {
        // Create a minimal env file so the backend has something to load.
        // No auto-generated API key — the backend mints per-user tokens
        // out-of-band via `python run.py users add` and the bootstrap
        // admin token written to data/BOOTSTRAP_TOKEN.txt.
        let content = "# Nymeria Configuration (auto-generated)\n\
                       # See .env.example for all available options\n\
                       \n\
                       # LLM Provider: any registry id (anthropic, openai, openrouter, google,\n\
                       # groq, deepseek, ...); see Nymeria/nymeria/config/llm_providers.py\n\
                       LLM_PROVIDER=anthropic\n\
                       \n\
                       # Add your API key here, or use CLIProxy for subscription-based access\n\
                       # ANTHROPIC_API_KEY=\n";

        if let Some(parent) = env_path.parent() {
            fs::create_dir_all(parent).map_err(|e| format!("Failed to create directory: {}", e))?;
        }

        fs::write(&env_path, content).map_err(|e| format!("Failed to create .env: {}", e))?;
    }

    // Pick up the bootstrap admin token if the agent has written one.
    // The file is a multi-line note, not a raw token — the actual token
    // lives on a `Token: nym_<...>` line. Returning the whole trimmed file
    // would produce an invalid Bearer value and silently 401 every request.
    let bootstrap_path = runtime_root.join("data").join("BOOTSTRAP_TOKEN.txt");
    if bootstrap_path.exists() {
        if let Ok(content) = fs::read_to_string(&bootstrap_path) {
            for line in content.lines() {
                let trimmed = line.trim();
                if let Some(value) = trimmed.strip_prefix("Token:") {
                    let token = value.trim().to_string();
                    if token.starts_with("nym_") {
                        return Ok(token);
                    }
                }
                // Fallback: if the file ever shrinks back to a raw token
                // (no "Token:" prefix), accept a bare nym_-prefixed line.
                if trimmed.starts_with("nym_") {
                    return Ok(trimmed.to_string());
                }
            }
        }
    }

    // No bootstrap token yet — the Setup Wizard will prompt for one.
    Ok(String::new())
}
