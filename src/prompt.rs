//! Prompt extraction from an OpenAI-shaped request body (SPEC §4.4).
//!
//! Deliberately narrow and entirely pure: no I/O, no config, no context. A
//! classifier filter must never become a request validator, so every
//! unrecognised shape — including invalid JSON — yields `None` rather than an
//! error.

use serde_json::Value;

/// Extract the prompt to classify from a raw request body.
///
/// Resolution order (first match wins):
/// 1. `messages[]` — the **last** element whose `role == "user"`; its `content`
///    is either a string, or an array of parts whose `type == "text"` joined
///    by newlines.
/// 2. `prompt` — a string, or an array of strings joined by newlines.
/// 3. `input` — a string, or an array of strings joined by newlines.
///
/// Returns `None` for a non-JSON body, a body that is not a JSON object, or a
/// body with none of the above (or whose extraction is blank after trimming).
#[must_use]
pub fn extract_prompt(body: &[u8]) -> Option<String> {
    let root: Value = serde_json::from_slice(body).ok()?;
    let object = root.as_object()?;

    let candidate = object
        .get("messages")
        .and_then(|m| from_messages(m.as_array()?))
        .or_else(|| object.get("prompt").and_then(text_or_array))
        .or_else(|| object.get("input").and_then(text_or_array))?;

    let trimmed = candidate.trim();
    if trimmed.is_empty() {
        return None;
    }
    Some(trimmed.to_owned())
}

/// Truncate `text` to at most `max_chars` characters on a **char boundary**.
///
/// A byte slice would panic in the middle of a multi-byte code point, which is
/// reachable from any request body, so the cut is counted in `char_indices`.
#[must_use]
pub fn truncate_chars(text: &str, max_chars: usize) -> &str {
    match text.char_indices().nth(max_chars) {
        Some((byte_index, _)) => &text[..byte_index],
        None => text,
    }
}

/// The `content` of the last `role == "user"` message.
fn from_messages(messages: &[Value]) -> Option<String> {
    messages
        .iter()
        .rev()
        .find(|m| m.get("role").and_then(Value::as_str) == Some("user"))
        .and_then(|m| m.get("content"))
        .and_then(content_text)
}

/// A message `content`: a plain string, or an array of `{type, text}` parts.
fn content_text(content: &Value) -> Option<String> {
    match content {
        Value::String(s) => Some(s.clone()),
        Value::Array(parts) => {
            let joined = parts
                .iter()
                .filter(|p| p.get("type").and_then(Value::as_str) == Some("text"))
                .filter_map(|p| p.get("text").and_then(Value::as_str))
                .collect::<Vec<_>>()
                .join("\n");
            (!joined.is_empty()).then_some(joined)
        },
        _ => None,
    }
}

/// A `prompt` / `input` field: a string, or an array of strings.
fn text_or_array(value: &Value) -> Option<String> {
    match value {
        Value::String(s) => Some(s.clone()),
        Value::Array(items) => {
            let joined = items.iter().filter_map(Value::as_str).collect::<Vec<_>>().join("\n");
            (!joined.is_empty()).then_some(joined)
        },
        _ => None,
    }
}
