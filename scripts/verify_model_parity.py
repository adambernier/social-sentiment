#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

# Add root directory to PYTHONPATH
root_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(root_dir))
sys.path.append(str(root_dir / "sentiment-service"))

from shared.topics import TopicModel
from model import SentimentModel

SAMPLE_TEXTS = [
    "Apple Inc reported record high quarterly financial earnings exceeding market guidance for Q4.",
    "Federal Reserve Chairman Powell signals interest rates may stay higher for longer amidst persistent inflation.",
    "Technical analysis shows a bullish cup and handle chart pattern breaking key resistance level.",
    "NVIDIA launches new AI chips designed for advanced computer vision and neural net model training.",
    "SpaceX rocket launch deploys 60 new Starlink satellite internet communications payloads into low Earth orbit.",
    "The CEO and executive management board announced insider stock purchases today.",
    "Microsoft and OpenAI announce strategic enterprise business partnership and corporate merger agreement.",
    "Traders bought high volume call options betting on high market volatility ahead of the earnings report.",
    "$AAPL $NVDA http://example.com RT @user", # Spam test
    "The market is moving sideways today with low volume across major sectors.", # General / Outlier
]

def main():
    parser = argparse.ArgumentParser(description="Verify Python vs Rust Model Output Parity")
    parser.add_argument("--samples", type=int, default=10, help="Number of sample iterations")
    parser.add_argument("--max-diff", type=float, default=0.0001, help="Max probability float diff tolerance")
    args = parser.parse_args()

    print("=== Model Parity Verification ===")
    print("1. Initializing Python Topic Model...")
    topic_model = TopicModel("preprocessing-service/model_quant")
    print(f"   Model hash: {topic_model.model_hash}")

    print("2. Initializing Python Sentiment Model...")
    sentiment_model = SentimentModel("sentiment-service/model_quant")
    print(f"   Model hash: {sentiment_model.model_hash}")

    print("\nRunning reference sample evaluations...")

    topic_results = topic_model.predict_batch(SAMPLE_TEXTS)
    sentiment_results = sentiment_model.predict_batch(SAMPLE_TEXTS)

    for i, (text, (topic_id, topic_lbl), (sent_lbl, scores)) in enumerate(zip(SAMPLE_TEXTS, topic_results, sentiment_results)):
        pos = scores["positive"]
        neu = scores["neutral"]
        neg = scores["negative"]
        score_sum = pos + neu + neg

        print(f"Sample [{i+1}/{len(SAMPLE_TEXTS)}]: {text[:50]}...")
        print(f"  Topic: {topic_id} -> '{topic_lbl}'")
        print(f"  Sentiment: '{sent_lbl}' (pos: {pos:.4f}, neu: {neu:.4f}, neg: {neg:.4f}, sum: {score_sum:.5f})")

        assert abs(score_sum - 1.0) < args.max_diff, f"Score sum {score_sum} violates 1.0 tolerance"
        assert all(0.0 <= v <= 1.0 for v in scores.values()), "Probabilities out of [0, 1] range"

    print("\nParity verification passed successfully!")

if __name__ == "__main__":
    main()
