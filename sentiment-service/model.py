from transformers import pipeline


class SentimentModel:
    def __init__(self, model_name: str = "StephanAkkerman/FinTwitBERT-sentiment"):
        print(f"Loading sentiment model: {model_name}...")
        self.pipe = pipeline("text-classification", model=model_name, top_k=None)
        
        # Map labels to our system's labels (positive/negative/neutral)
        # Using lowercase keys for case-insensitive matching
        self.label_map = {
            "bullish": "positive",
            "bearish": "negative",
            "neutral": "neutral"
        }

    def predict_batch(self, texts: list[str]) -> list[tuple[str, dict[str, float]]]:
        # Truncate all texts to 512 tokens (BERT limit)
        truncated_texts = [text[:512] for text in texts]
        
        # Batch process with the pipeline
        batch_results = self.pipe(truncated_texts)
        
        final_results = []
        for results in batch_results:
            # Extract and map scores (case-insensitive)
            scores = {}
            for r in results:
                raw_label = r["label"]
                mapped_label = self.label_map.get(raw_label.lower(), raw_label.lower())
                scores[mapped_label] = r["score"]
            
            # Pick the winner
            top_label = max(scores, key=scores.get)
            final_results.append((top_label, scores))
            
        return final_results
