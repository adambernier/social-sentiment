use anyhow::Result;
use ort::session::Session;
use ort::value::Value;
use social_sentiment_core::config::OrtThreadConfig;
use social_sentiment_core::model_identity::sha256_file;
use std::collections::HashMap;
use std::path::Path;
use std::sync::Mutex;
use tokenizers::{EncodeInput, Tokenizer};

pub struct TopicModel {
    tokenizer: Tokenizer,
    session: Mutex<Session>,
    pub version: String,
    pub model_hash: String,
    pub entailment_threshold: f32,
    candidate_labels: Vec<&'static str>,
    hypotheses: Vec<String>,
    label_map: HashMap<&'static str, &'static str>,
}

fn configured_entailment_threshold() -> f32 {
    if let Ok(raw) = std::env::var("TOPIC_CONFORMAL_Q_HAT") {
        if let Ok(q_hat) = raw.trim().parse::<f64>() {
            if let Some(threshold) =
                social_sentiment_math::conformal::conformal_topic_threshold(q_hat)
            {
                return threshold;
            }
        }
    }
    if let Ok(raw) = std::env::var("TOPIC_ENTAILMENT_THRESHOLD") {
        if let Ok(threshold) = raw.trim().parse::<f32>() {
            if threshold.is_finite() && (0.0..=1.0).contains(&threshold) {
                return threshold;
            }
        }
    }
    0.60
}

fn is_substantive_text(text: &str) -> bool {
    let mut non_spam_count = 0usize;
    for w in text.split_whitespace() {
        let w_clean = w.trim().to_lowercase();
        if w_clean.starts_with('$')
            || w_clean.starts_with('@')
            || w_clean.contains("http")
            || w_clean == "rt"
            || w_clean == "via"
        {
            continue;
        }
        if w_clean.chars().any(char::is_alphanumeric) {
            non_spam_count += 1;
            if non_spam_count >= 3 {
                return true;
            }
        }
    }
    false
}

impl TopicModel {
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

        tokenizer.with_padding(Some(tokenizers::PaddingParams {
            strategy: tokenizers::PaddingStrategy::BatchLongest,
            direction: tokenizers::PaddingDirection::Right,
            pad_to_multiple_of: None,
            pad_id: 0,
            pad_type_id: 0,
            pad_token: "[PAD]".to_string(),
        }));

        let threads = OrtThreadConfig::from_env();
        let session = Session::builder()?
            .with_intra_threads(threads.intra_threads)
            .map_err(|e| anyhow::anyhow!("Failed to set ORT intra_threads: {e}"))?
            .with_inter_threads(threads.inter_threads)
            .map_err(|e| anyhow::anyhow!("Failed to set ORT inter_threads: {e}"))?
            .commit_from_file(&model_path)?;

        let model_hash = sha256_file(&model_path)?;
        let version = std::env::var("TOPIC_MODEL_VERSION")
            .unwrap_or_else(|_| "zero-shot-onnx-v1".to_string());

        let candidate_labels = vec![
            "financial earnings",
            "federal reserve and interest rates",
            "technical analysis and stock charts",
            "artificial intelligence and computer technology",
            "space exploration and satellites",
            "company management and leadership",
            "business partnerships and corporate mergers",
            "options trading",
        ];

        let hypotheses = candidate_labels
            .iter()
            .map(|label| format!("This text is about {label}."))
            .collect();

        let mut label_map = HashMap::new();
        label_map.insert("financial earnings", "Earnings & Guidance");
        label_map.insert("federal reserve and interest rates", "Fed & Macro");
        label_map.insert("technical analysis and stock charts", "Technical Analysis");
        label_map.insert(
            "artificial intelligence and computer technology",
            "AI & Compute",
        );
        label_map.insert("space exploration and satellites", "Space & Satellite");
        label_map.insert("company management and leadership", "Management & Insider");
        label_map.insert(
            "business partnerships and corporate mergers",
            "M&A & Partnerships",
        );
        label_map.insert("options trading", "Options & Volatility");

        Ok(Self {
            tokenizer,
            session: Mutex::new(session),
            version,
            model_hash,
            entailment_threshold: configured_entailment_threshold(),
            candidate_labels,
            hypotheses,
            label_map,
        })
    }

    pub fn predict(&self, text: &str) -> Result<(i32, String)> {
        let (topic_id, topic_label, _) = self.predict_with_scores(text)?;
        Ok((topic_id, topic_label))
    }

    pub fn predict_with_scores(&self, text: &str) -> Result<(i32, String, Vec<f32>)> {
        let mut results = self.predict_batch_with_scores(&[text.to_string()])?;
        results
            .pop()
            .ok_or_else(|| anyhow::anyhow!("topic batch returned empty result for single input"))
    }

    pub fn predict_batch(&self, texts: &[String]) -> Result<Vec<(i32, String)>> {
        let scored = self.predict_batch_with_scores(texts)?;
        Ok(scored
            .into_iter()
            .map(|(topic_id, topic_label, _)| (topic_id, topic_label))
            .collect())
    }

    pub fn predict_batch_with_scores(
        &self,
        texts: &[String],
    ) -> Result<Vec<(i32, String, Vec<f32>)>> {
        if texts.is_empty() {
            return Ok(Vec::new());
        }

        let mut results: Vec<(i32, String, Vec<f32>)> =
            vec![(-1, "General / Outlier".to_string(), Vec::new()); texts.len()];
        let active_indices: Vec<usize> = texts
            .iter()
            .enumerate()
            .filter_map(|(idx, text)| is_substantive_text(text).then_some(idx))
            .collect();

        if active_indices.is_empty() {
            return Ok(results);
        }

        let num_labels = self.candidate_labels.len();
        let total_pairs = active_indices.len() * num_labels;
        let mut inputs = Vec::with_capacity(total_pairs);
        for &idx in &active_indices {
            let text = texts[idx].as_str();
            for hypo in &self.hypotheses {
                inputs.push(EncodeInput::Dual(text.into(), hypo.as_str().into()));
            }
        }

        let encodings = self
            .tokenizer
            .encode_batch(inputs, true)
            .map_err(|e| anyhow::anyhow!("Tokenization failed: {}", e))?;

        let max_len = encodings
            .iter()
            .map(|e| e.get_ids().len())
            .max()
            .unwrap_or(0);

        let mut input_ids_vec = vec![0i64; total_pairs * max_len];
        let mut attention_mask_vec = vec![0i64; total_pairs * max_len];

        for (i, encoding) in encodings.iter().enumerate() {
            for (j, &id) in encoding.get_ids().iter().enumerate() {
                input_ids_vec[i * max_len + j] = id as i64;
            }
            for (j, &mask) in encoding.get_attention_mask().iter().enumerate() {
                attention_mask_vec[i * max_len + j] = mask as i64;
            }
        }

        let shape = vec![total_pairs as i64, max_len as i64];
        let input_ids_tensor = Value::from_array((shape.clone(), input_ids_vec))?;
        let attention_mask_tensor = Value::from_array((shape, attention_mask_vec))?;

        let mut session_guard = self
            .session
            .lock()
            .map_err(|_| anyhow::anyhow!("ONNX session lock poisoned"))?;

        let outputs = session_guard.run(ort::inputs![
            "input_ids" => input_ids_tensor,
            "attention_mask" => attention_mask_tensor
        ])?;

        let (out_shape, logits) = outputs[0].try_extract_tensor::<f32>()?;
        if out_shape.len() < 2 || out_shape[0] != total_pairs as i64 || out_shape[1] < 3 {
            return Err(anyhow::anyhow!("Unexpected logits shape: {:?}", out_shape));
        }
        let num_classes = out_shape[1] as usize;

        for (active_pos, &orig_idx) in active_indices.iter().enumerate() {
            let mut entail_probs = Vec::with_capacity(num_labels);
            for label_idx in 0..num_labels {
                let pair_idx = active_pos * num_labels + label_idx;
                let offset = pair_idx * num_classes;
                let entail = logits[offset];
                let contra = logits[offset + 2];

                let entail_prob =
                    social_sentiment_math::postprocess::topic_entailment_prob(entail, contra)
                        .ok_or_else(|| {
                            anyhow::anyhow!(
                        "Non-finite topic logits for item {orig_idx}, candidate index {label_idx}"
                    )
                        })?;
                entail_probs.push(entail_prob);
            }

            results[orig_idx] = match social_sentiment_math::postprocess::select_topic_index(
                &entail_probs,
                self.entailment_threshold,
            ) {
                None => (-1, "General / Outlier".to_string(), entail_probs),
                Some(best_idx) => {
                    let desc_label = self.candidate_labels[best_idx];
                    let mapped_label = self.label_map.get(desc_label).unwrap_or(&desc_label);
                    (best_idx as i32, mapped_label.to_string(), entail_probs)
                }
            };
        }

        Ok(results)
    }
}

#[cfg(test)]
mod tests {
    use super::is_substantive_text;

    #[test]
    fn test_topic_model_spam_filter() {
        assert!(!is_substantive_text(
            "$AAPL @user http://example.com RT via"
        ));
        assert!(!is_substantive_text("$NVDA moon!!"));
        assert!(is_substantive_text(
            "$NVDA quarterly earnings beat Wall Street guidance"
        ));
    }
}
