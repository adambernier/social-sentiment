use anyhow::Result;
use serde_json::json;
use service_preprocessing::topic_model::TopicModel;
use std::io::{self, Read};

fn main() -> Result<()> {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input)?;
    let texts: Vec<String> = serde_json::from_str(&input)?;
    let model_dir = std::env::var("TOPIC_MODEL_DIR")
        .unwrap_or_else(|_| "preprocessing-service/model_quant".to_string());
    let model = TopicModel::new(model_dir)?;
    let scored_results = texts
        .iter()
        .map(|text| model.predict_with_scores(text))
        .collect::<Result<Vec<_>>>()?;
    let results = scored_results
        .iter()
        .map(|(topic_id, topic_label, _)| (*topic_id, topic_label.clone()))
        .collect::<Vec<_>>();
    let scores = scored_results
        .into_iter()
        .map(|(_, _, scores)| scores)
        .collect::<Vec<_>>();
    serde_json::to_writer(
        io::stdout(),
        &json!({
            "model_hash": model.model_hash,
            "model_version": model.version,
            "results": results,
            "scores": scores
        }),
    )?;
    Ok(())
}
