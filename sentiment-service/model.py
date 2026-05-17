import numpy as np
import onnxruntime as ort
from transformers import AutoTokenizer

class SentimentModel:
    def __init__(self, model_dir: str = "sentiment-service/model_quant"):
        print(f"Loading quantized ONNX sentiment model from {model_dir}...")
        self.tokenizer = AutoTokenizer.from_pretrained(model_dir)
        self.session = ort.InferenceSession(f"{model_dir}/model_quant.onnx")
        
        # ID mapping from FinTwitBERT config: {0: 'NEUTRAL', 1: 'BULLISH', 2: 'BEARISH'}
        self.id2label = {0: 'neutral', 1: 'positive', 2: 'negative'}

    def predict_batch(self, texts: list[str]) -> list[tuple[str, dict[str, float]]]:
        # Truncate all texts to 512 tokens
        inputs = self.tokenizer(
            texts, 
            padding=True, 
            truncation=True, 
            max_length=512, 
            return_tensors="np"
        )
        
        # Convert to int64 for ONNX
        onnx_inputs = {k: v.astype(np.int64) for k, v in inputs.items()}
        
        # Run inference
        logits = self.session.run(None, onnx_inputs)[0]
        
        # Softmax and results
        probs = np.exp(logits) / np.sum(np.exp(logits), axis=-1, keepdims=True)
        
        final_results = []
        for i in range(len(texts)):
            top_idx = np.argmax(probs[i])
            top_label = self.id2label[top_idx]
            
            # Construct scores dict for ScoredPost
            scores = {
                "neutral": float(probs[i][0]),
                "positive": float(probs[i][1]),
                "negative": float(probs[i][2])
            }
            final_results.append((top_label, scores))
            
        return final_results
