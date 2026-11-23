use anyhow::Result;
use ort::session::Session;
use ort::value::Value;
use social_sentiment_core::config::OrtThreadConfig;
use social_sentiment_core::model_identity::sha256_file;
use std::collections::HashMap;
use std::path::Path;
use std::sync::Mutex;
use tokenizers::{EncodeInput, Tokenizer};

const MAX_PADDED_TOKENS_PER_FORWARD: usize = 4_096;

/// Group sequence indices into length-sorted sub-batches whose padded tensor area
/// (`chunk.len() * chunk_max_len`) stays within `max_padded_tokens` so one long
/// news article never forces 31 short social posts to pad out to 512 tokens.
fn plan_token_budget_chunks(lengths: &[usize], max_padded_tokens: usize) -> Vec<Vec<usize>> {
    if lengths.is_empty() {
        return Vec::new();
    }
    let mut order: Vec<usize> = (0..lengths.len()).collect();
    order.sort_by_key(|&idx| lengths[idx]);

    let mut chunks = Vec::new();
    let mut current = Vec::new();

    for idx in order {
        let projected_tokens = (current.len() + 1).saturating_mul(lengths[idx]);
        if !current.is_empty() && projected_tokens > max_padded_tokens {
            chunks.push(std::mem::take(&mut current));
        }
        current.push(idx);
    }
    if !current.is_empty() {
        chunks.push(current);
    }
    chunks
}

pub struct SentimentModel {
    tokenizer: Tokenizer,
    session: Mutex<Session>,
    pub version: String,
    pub model_hash: String,
}

impl SentimentModel {
    pub fn new<P: AsRef<Path>>(model_dir: P) -> Result<Self> {
        let model_dir_ref = model_dir.as_ref();
        let tokenizer_path = model_dir_ref.join("tokenizer.json");
        let model_path = model_dir_ref.join("model_quant.onnx");

        let mut tokenizer = Tokenizer::from_file(&tokenizer_path).map_err(|e| {
            anyhow::anyhow!("Failed to load tokenizer from {:?}: {}", tokenizer_path, e)
        })?;

        tokenizer
            .with_truncation(Some(tokenizers::TruncationParams {
                max_length: 512,
                strategy: tokenizers::TruncationStrategy::LongestFirst,
                stride: 0,
                direction: tokenizers::TruncationDirection::Right,
            }))
            .map_err(|e| anyhow::anyhow!("Failed to set truncation: {}", e))?;

        // Keep encodings unpadded so we can pack length-sorted sub-batches and
        // pad (id=0, mask=0, type_id=0) per chunk.
        tokenizer.with_padding(None);

        let threads = OrtThreadConfig::from_env();
        let session = Session::builder()?
            .with_intra_threads(threads.intra_threads)
            .map_err(|e| anyhow::anyhow!("Failed to set ORT intra_threads: {e}"))?
            .with_inter_threads(threads.inter_threads)
            .map_err(|e| anyhow::anyhow!("Failed to set ORT inter_threads: {e}"))?
            .commit_from_file(&model_path)?;

        let model_hash = sha256_file(&model_path)?;
        let version = std::env::var("SENTIMENT_MODEL_VERSION")
            .unwrap_or_else(|_| "fintwitbert-onnx-v1".to_string());

        Ok(Self {
            tokenizer,
            session: Mutex::new(session),
            version,
            model_hash,
        })
    }

    pub fn predict_batch(&self, texts: &[String]) -> Result<Vec<(String, HashMap<String, f64>)>> {
        if texts.is_empty() {
            return Ok(Vec::new());
        }

        let inputs: Vec<EncodeInput> = texts
            .iter()
            .map(|t| EncodeInput::Single(t.as_str().into()))
            .collect();

        let encodings = self
            .tokenizer
            .encode_batch(inputs, true)
            .map_err(|e| anyhow::anyhow!("Tokenization failed: {}", e))?;

        let lengths: Vec<usize> = encodings.iter().map(|e| e.get_ids().len()).collect();
        let chunks = plan_token_budget_chunks(&lengths, MAX_PADDED_TOKENS_PER_FORWARD);

        let mut session_guard = self
            .session
            .lock()
            .map_err(|_| anyhow::anyhow!("ONNX session lock poisoned"))?;

        let mut results: Vec<Option<(String, HashMap<String, f64>)>> = vec![None; texts.len()];

        for chunk in chunks {
            let chunk_size = chunk.len();
            let max_len = chunk.iter().map(|&idx| lengths[idx]).max().unwrap_or(0);

            let mut input_ids_vec = vec![0i64; chunk_size * max_len];
            let mut attention_mask_vec = vec![0i64; chunk_size * max_len];
            let mut token_type_ids_vec = vec![0i64; chunk_size * max_len];

            for (row_idx, &orig_idx) in chunk.iter().enumerate() {
                let encoding = &encodings[orig_idx];
                let row_offset = row_idx * max_len;
                for (j, &id) in encoding.get_ids().iter().enumerate() {
                    input_ids_vec[row_offset + j] = id as i64;
                }
                for (j, &mask) in encoding.get_attention_mask().iter().enumerate() {
                    attention_mask_vec[row_offset + j] = mask as i64;
                }
                for (j, &type_id) in encoding.get_type_ids().iter().enumerate() {
                    token_type_ids_vec[row_offset + j] = type_id as i64;
                }
            }

            let shape = vec![chunk_size as i64, max_len as i64];
            let input_ids_tensor = Value::from_array((shape.clone(), input_ids_vec))?;
            let attention_mask_tensor = Value::from_array((shape.clone(), attention_mask_vec))?;
            let token_type_ids_tensor = Value::from_array((shape, token_type_ids_vec))?;

            let outputs = session_guard.run(ort::inputs![
                "input_ids" => input_ids_tensor,
                "attention_mask" => attention_mask_tensor,
                "token_type_ids" => token_type_ids_tensor
            ])?;

            let (out_shape, logits) = outputs[0].try_extract_tensor::<f32>()?;
            if out_shape.len() < 2 || out_shape[0] as usize != chunk_size || out_shape[1] < 3 {
                return Err(anyhow::anyhow!("Unexpected logits shape: {:?}", out_shape));
            }
            let num_classes = out_shape[1] as usize;

            for (row_idx, &orig_idx) in chunk.iter().enumerate() {
                let offset = row_idx * num_classes;
                let l0 = logits[offset]; // neutral
                let l1 = logits[offset + 1]; // positive
                let l2 = logits[offset + 2]; // negative

                let pred =
                    social_sentiment_math::postprocess::sentiment_softmax_and_label([l0, l1, l2])
                        .ok_or_else(|| {
                        anyhow::anyhow!("Non-finite sentiment logits for batch item {orig_idx}")
                    })?;

                let mut scores = HashMap::new();
                scores.insert("neutral".to_string(), pred.neutral);
                scores.insert("positive".to_string(), pred.positive);
                scores.insert("negative".to_string(), pred.negative);

                results[orig_idx] = Some((pred.label.as_str().to_string(), scores));
            }
        }

        results
            .into_iter()
            .enumerate()
            .map(|(idx, item)| {
                item.ok_or_else(|| anyhow::anyhow!("Missing sentiment prediction for item {idx}"))
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::plan_token_budget_chunks;
    use social_sentiment_math::postprocess::{sentiment_softmax_and_label, SentimentLabel};

    #[test]
    fn test_softmax_calculation() {
        let pred = sentiment_softmax_and_label([1.0, 2.0, 0.5]).expect("finite logits");
        assert!((pred.neutral + pred.positive + pred.negative - 1.0).abs() < 1e-12);
        assert_eq!(pred.label, SentimentLabel::Positive);
        assert!(pred.positive > pred.neutral && pred.positive > pred.negative);
    }

    #[test]
    fn token_budget_prevents_single_long_post_from_padding_entire_batch() {
        let mut lengths = vec![30; 31];
        lengths.push(512);
        // 31 * 30 = 930 <= 4096 (chunk 1); adding the 512-token item would jump to
        // 32 * 512 = 16384 > 4096, so it splits cleanly into its own chunk.
        let chunks = plan_token_budget_chunks(&lengths, 4_096);
        assert_eq!(chunks.len(), 2);
        assert_eq!(chunks[0].len(), 31);
        assert_eq!(chunks[1], vec![31]);
    }
}
