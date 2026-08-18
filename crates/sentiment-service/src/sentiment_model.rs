use std::collections::HashMap;
use std::path::Path;
use std::sync::Mutex;
use anyhow::Result;
use ort::session::Session;
use ort::value::Value;
use social_sentiment_core::model_identity::sha256_file;
use tokenizers::{EncodeInput, Tokenizer};

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

        let batch_size = texts.len();
        let max_len = encodings.iter().map(|e| e.get_ids().len()).max().unwrap_or(0);

        let mut input_ids_vec = vec![0i64; batch_size * max_len];
        let mut attention_mask_vec = vec![0i64; batch_size * max_len];

        for (i, encoding) in encodings.iter().enumerate() {
            for (j, &id) in encoding.get_ids().iter().enumerate() {
                input_ids_vec[i * max_len + j] = id as i64;
            }
            for (j, &mask) in encoding.get_attention_mask().iter().enumerate() {
                attention_mask_vec[i * max_len + j] = mask as i64;
            }
        }

        let shape = vec![batch_size as i64, max_len as i64];
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
        if out_shape.len() < 2 || out_shape[0] as usize != batch_size || out_shape[1] < 3 {
            return Err(anyhow::anyhow!("Unexpected logits shape: {:?}", out_shape));
        }

        let mut results = Vec::with_capacity(batch_size);

        for i in 0..batch_size {
            let offset = i * 3;
            let l0 = logits[offset + 0]; // neutral
            let l1 = logits[offset + 1]; // positive
            let l2 = logits[offset + 2]; // negative

            let max_l = l0.max(l1).max(l2);
            let e0 = (l0 - max_l).exp();
            let e1 = (l1 - max_l).exp();
            let e2 = (l2 - max_l).exp();
            let sum_e = e0 + e1 + e2;

            let p0 = (e0 / sum_e) as f64; // neutral
            let p1 = (e1 / sum_e) as f64; // positive
            let p2 = (e2 / sum_e) as f64; // negative

            let (top_label, _top_p) = if p1 >= p0 && p1 >= p2 {
                ("positive", p1)
            } else if p2 >= p0 && p2 >= p1 {
                ("negative", p2)
            } else {
                ("neutral", p0)
            };

            let mut scores = HashMap::new();
            scores.insert("neutral".to_string(), p0);
            scores.insert("positive".to_string(), p1);
            scores.insert("negative".to_string(), p2);

            // Minor rounding adjustment to ensure exact sum = 1.0 within float precision
            let sum_p = p0 + p1 + p2;
            if (sum_p - 1.0).abs() > 1e-6 {
                let diff = 1.0 - sum_p;
                if let Some(val) = scores.get_mut(top_label) {
                    *val += diff;
                }
            }

            results.push((top_label.to_string(), scores));
        }

        Ok(results)
    }
}

#[cfg(test)]
mod tests {

    #[test]
    fn test_softmax_calculation() {
        let l0 = 1.0f32;
        let l1 = 2.0f32;
        let l2 = 0.5f32;
        let max_l = l0.max(l1).max(l2);
        let e0 = (l0 - max_l).exp();
        let e1 = (l1 - max_l).exp();
        let e2 = (l2 - max_l).exp();
        let sum_e = e0 + e1 + e2;

        let p0 = (e0 / sum_e) as f64;
        let p1 = (e1 / sum_e) as f64;
        let p2 = (e2 / sum_e) as f64;

        assert!((p0 + p1 + p2 - 1.0).abs() < 1e-5);
        assert!(p1 > p0 && p1 > p2);
    }
}
