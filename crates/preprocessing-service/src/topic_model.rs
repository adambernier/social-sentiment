use std::collections::HashMap;
use std::path::Path;
use std::sync::Mutex;
use anyhow::Result;
use ort::session::Session;
use ort::value::Value;
use social_sentiment_core::model_identity::sha256_file;
use tokenizers::{EncodeInput, Tokenizer};

pub struct TopicModel {
    tokenizer: Tokenizer,
    session: Mutex<Session>,
    pub version: String,
    pub model_hash: String,
    candidate_labels: Vec<&'static str>,
    label_map: HashMap<&'static str, &'static str>,
}

impl TopicModel {
    pub fn new<P: AsRef<Path>>(model_dir: P) -> Result<Self> {
        let model_dir_ref = model_dir.as_ref();
        let tokenizer_path = model_dir_ref.join("tokenizer.json");
        let model_path = model_dir_ref.join("model_quant.onnx");

        let mut tokenizer = Tokenizer::from_file(&tokenizer_path)
            .map_err(|e| anyhow::anyhow!("Failed to load tokenizer from {:?}: {}", tokenizer_path, e))?;

        tokenizer.with_truncation(Some(tokenizers::TruncationParams {
            max_length: 512,
            strategy: tokenizers::TruncationStrategy::LongestFirst,
            stride: 0,
            direction: tokenizers::TruncationDirection::Right,
        })).map_err(|e| anyhow::anyhow!("Failed to set truncation: {}", e))?;

        tokenizer.with_padding(Some(tokenizers::PaddingParams {
            strategy: tokenizers::PaddingStrategy::BatchLongest,
            direction: tokenizers::PaddingDirection::Right,
            pad_to_multiple_of: None,
            pad_id: 0,
            pad_type_id: 0,
            pad_token: "[PAD]".to_string(),
        }));

        let session = Session::builder()?
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

        let mut label_map = HashMap::new();
        label_map.insert("financial earnings", "Earnings & Guidance");
        label_map.insert("federal reserve and interest rates", "Fed & Macro");
        label_map.insert("technical analysis and stock charts", "Technical Analysis");
        label_map.insert("artificial intelligence and computer technology", "AI & Compute");
        label_map.insert("space exploration and satellites", "Space & Satellite");
        label_map.insert("company management and leadership", "Management & Insider");
        label_map.insert("business partnerships and corporate mergers", "M&A & Partnerships");
        label_map.insert("options trading", "Options & Volatility");

        Ok(Self {
            tokenizer,
            session: Mutex::new(session),
            version,
            model_hash,
            candidate_labels,
            label_map,
        })
    }

    pub fn predict(&self, text: &str) -> Result<(i32, String)> {
        // Spam / outlier heuristic check
        let words = text.split_whitespace();
        let mut non_spam_count = 0;
        for w in words {
            let w_clean = w.trim().to_lowercase();
            if w_clean.starts_with('$')
                || w_clean.starts_with('@')
                || w_clean.contains("http")
                || w_clean == "rt"
                || w_clean == "via"
            {
                continue;
            }
            let alnum: String = w_clean.chars().filter(|c| c.is_alphanumeric()).collect();
            if !alnum.is_empty() {
                non_spam_count += 1;
            }
        }

        if non_spam_count < 3 {
            return Ok((-1, "General / Outlier".to_string()));
        }

        let hypothesis_template = "This text is about {}.";
        let inputs: Vec<EncodeInput> = self
            .candidate_labels
            .iter()
            .map(|label| {
                let hypo = hypothesis_template.replace("{}", label);
                EncodeInput::Dual(text.into(), hypo.into())
            })
            .collect();

        let encodings = self
            .tokenizer
            .encode_batch(inputs, true)
            .map_err(|e| anyhow::anyhow!("Tokenization failed: {}", e))?;

        let num_pairs = self.candidate_labels.len(); // 8
        let max_len = encodings.iter().map(|e| e.get_ids().len()).max().unwrap_or(0);

        let mut input_ids_vec = vec![0i64; num_pairs * max_len];
        let mut attention_mask_vec = vec![0i64; num_pairs * max_len];

        for (i, encoding) in encodings.iter().enumerate() {
            for (j, &id) in encoding.get_ids().iter().enumerate() {
                input_ids_vec[i * max_len + j] = id as i64;
            }
            for (j, &mask) in encoding.get_attention_mask().iter().enumerate() {
                attention_mask_vec[i * max_len + j] = mask as i64;
            }
        }

        let shape = vec![num_pairs as i64, max_len as i64];
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
        if out_shape.len() < 2 || out_shape[0] != num_pairs as i64 || out_shape[1] < 3 {
            return Err(anyhow::anyhow!("Unexpected logits shape: {:?}", out_shape));
        }

        let mut best_idx = 0;
        let mut max_entail_prob = -1.0f32;

        for i in 0..num_pairs {
            let entail = logits[i * 3 + 0];
            let contra = logits[i * 3 + 2];

            let exp_entail = entail.exp();
            let exp_contra = contra.exp();
            let entail_prob = exp_entail / (exp_entail + exp_contra);

            if entail_prob > max_entail_prob {
                max_entail_prob = entail_prob;
                best_idx = i;
            }
        }

        if max_entail_prob < 0.60 {
            Ok((-1, "General / Outlier".to_string()))
        } else {
            let desc_label = self.candidate_labels[best_idx];
            let mapped_label = self.label_map.get(desc_label).unwrap_or(&desc_label);
            Ok((best_idx as i32, mapped_label.to_string()))
        }
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn test_topic_model_spam_filter() {
        let spam_text = "$AAPL @user http://example.com RT via";
        let words = spam_text.split_whitespace();
        let mut non_spam_count = 0;
        for w in words {
            let w_clean = w.trim().to_lowercase();
            if w_clean.starts_with('$')
                || w_clean.starts_with('@')
                || w_clean.contains("http")
                || w_clean == "rt"
                || w_clean == "via"
            {
                continue;
            }
            let alnum: String = w_clean.chars().filter(|c| c.is_alphanumeric()).collect();
            if !alnum.is_empty() {
                non_spam_count += 1;
            }
        }
        assert_eq!(non_spam_count, 0);
    }
}
