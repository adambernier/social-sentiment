use anyhow::Result;
use serde_json::json;
use service_sentiment::sentiment_model::SentimentModel;
use std::io::{self, Read};

fn main() -> Result<()> {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input)?;
    let texts: Vec<String> = serde_json::from_str(&input)?;
    let model_dir = std::env::var("SENTIMENT_MODEL_DIR")
        .unwrap_or_else(|_| "sentiment-service/model_quant".to_string());
    let model = SentimentModel::new(model_dir)?;
    let results = model.predict_batch(&texts)?;
    serde_json::to_writer(
        io::stdout(),
        &json!({
            "model_hash": model.model_hash,
            "model_version": model.version,
            "results": results
        }),
    )?;
    Ok(())
}
