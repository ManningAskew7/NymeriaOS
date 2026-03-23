//! First-run auto-configuration: generates NYMERIA_API_KEY and ensures .env exists.

use std::fs;
use std::path::Path;

/// Ensure the .env file exists and contains a NYMERIA_API_KEY.
/// Returns the API key (existing or newly generated).
pub fn ensure_env_file(project_root: &Path) -> Result<String, String> {
    let env_path = project_root.join("Nymeria").join(".env");

    if env_path.exists() {
        // Try to read existing API key
        let contents = fs::read_to_string(&env_path)
            .map_err(|e| format!("Failed to read .env: {}", e))?;

        for line in contents.lines() {
            let trimmed = line.trim();
            if trimmed.starts_with("NYMERIA_API_KEY=") {
                let key = trimmed
                    .strip_prefix("NYMERIA_API_KEY=")
                    .unwrap_or("")
                    .trim()
                    .trim_matches('"')
                    .trim_matches('\'');
                if !key.is_empty() {
                    return Ok(key.to_string());
                }
            }
        }

        // .env exists but no NYMERIA_API_KEY — append one
        let api_key = generate_api_key();
        let mut contents = contents;
        if !contents.ends_with('\n') {
            contents.push('\n');
        }
        contents.push_str(&format!("NYMERIA_API_KEY={}\n", api_key));
        fs::write(&env_path, contents)
            .map_err(|e| format!("Failed to update .env: {}", e))?;
        return Ok(api_key);
    }

    // No .env at all — create a minimal one
    let api_key = generate_api_key();
    let content = format!(
        "# Nymeria Configuration (auto-generated)\n\
         # See .env.example for all available options\n\
         \n\
         NYMERIA_API_KEY={}\n\
         \n\
         # LLM Provider (anthropic, openai, openrouter)\n\
         LLM_PROVIDER=anthropic\n\
         \n\
         # Add your API key here, or use CLIProxy for subscription-based access\n\
         # ANTHROPIC_API_KEY=\n",
        api_key
    );

    // Ensure parent directory exists
    if let Some(parent) = env_path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("Failed to create directory: {}", e))?;
    }

    fs::write(&env_path, content)
        .map_err(|e| format!("Failed to create .env: {}", e))?;

    Ok(api_key)
}

fn generate_api_key() -> String {
    format!("nymeria-{}", uuid::Uuid::new_v4())
}
