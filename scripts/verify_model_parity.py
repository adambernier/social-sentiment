import argparse
import json

# The subprocess calls below use fixed cargo argument vectors without a shell.
import subprocess  # nosec B404
import sys
from pathlib import Path

# Add root directory to PYTHONPATH
root_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(root_dir))
sys.path.append(str(root_dir / "sentiment-service"))

from model import SentimentModel

from shared.topics import TopicModel

SAMPLE_TEXTS = [
    "Apple Inc reported record high quarterly financial earnings exceeding market guidance for Q4.",
    "Federal Reserve Chairman Powell signals interest rates may stay higher for longer amidst persistent inflation.",
    "Technical analysis shows a bullish cup and handle chart pattern breaking key resistance level.",
    "NVIDIA launches new AI chips designed for advanced computer vision and neural net model training.",
    "SpaceX rocket launch deploys 60 new Starlink satellite internet communications payloads into low Earth orbit.",
    "The CEO and executive management board announced insider stock purchases today.",
    "Microsoft and OpenAI announce strategic enterprise business partnership and corporate merger agreement.",
    "Traders bought high volume call options betting on high market volatility ahead of the earnings report.",
    "$AAPL $NVDA http://example.com RT @user",  # Spam test
    "The market is moving sideways today with low volume across major sectors.",  # General / Outlier
]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(
        description="Verify Python vs Rust model output parity"
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=len(SAMPLE_TEXTS),
        help="Number of fixture texts to compare",
    )
    parser.add_argument(
        "--max-diff",
        type=float,
        default=0.04,
        help="Maximum cross-runtime probability difference (default: 0.04)",
    )
    args = parser.parse_args()

    print("=== Model Parity Verification ===")
    print("1. Initializing Python Topic Model...")
    topic_model = TopicModel("preprocessing-service/model_quant")
    print(f"   Model hash: {topic_model.model_hash}")

    print("2. Initializing Python Sentiment Model...")
    sentiment_model = SentimentModel("sentiment-service/model_quant")
    print(f"   Model hash: {sentiment_model.model_hash}")

    if not 1 <= args.samples <= len(SAMPLE_TEXTS):
        parser.error(f"--samples must be between 1 and {len(SAMPLE_TEXTS)}")
    sample_texts = SAMPLE_TEXTS[: args.samples]

    print("\n3. Running Python reference evaluations...")

    topic_results = topic_model.predict_batch(sample_texts)
    sentiment_results = sentiment_model.predict_batch(sample_texts)

    def run_rust(package: str, binary: str):
        command = [
            "cargo",
            "run",
            "--quiet",
            "--offline",
            "-p",
            package,
            "--bin",
            binary,
        ]
        completed = subprocess.run(  # nosec B603
            command,
            cwd=root_dir,
            input=json.dumps(sample_texts),
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"{' '.join(command)} failed:\n{completed.stderr.strip()}"
            )
        return json.loads(completed.stdout)

    print("4. Running Rust evaluations...")
    rust_topic = run_rust("service-preprocessing", "topic-infer")
    rust_sentiment = run_rust("service-sentiment", "sentiment-infer")

    require(
        rust_topic["model_hash"] == topic_model.model_hash,
        "Topic model hashes differ",
    )
    require(
        rust_sentiment["model_hash"] == sentiment_model.model_hash,
        "Sentiment model hashes differ",
    )

    for i, (
        text,
        (topic_id, topic_lbl),
        (sent_lbl, scores),
        rust_topic_result,
        rust_sentiment_result,
    ) in enumerate(
        zip(
            sample_texts,
            topic_results,
            sentiment_results,
            rust_topic["results"],
            rust_sentiment["results"],
        )
    ):
        pos = scores["positive"]
        neu = scores["neutral"]
        neg = scores["negative"]
        score_sum = pos + neu + neg

        print(f"Sample [{i + 1}/{len(sample_texts)}]: {text[:50]}...")
        print(f"  Topic: {topic_id} -> '{topic_lbl}'")
        print(
            f"  Sentiment: '{sent_lbl}' (pos: {pos:.4f}, neu: {neu:.4f}, neg: {neg:.4f}, sum: {score_sum:.5f})"
        )

        require(
            abs(score_sum - 1.0) < args.max_diff,
            f"Score sum {score_sum} violates 1.0 tolerance",
        )
        require(
            all(0.0 <= v <= 1.0 for v in scores.values()),
            "Probabilities out of [0, 1] range",
        )
        require(
            rust_topic_result == [topic_id, topic_lbl],
            f"Topic mismatch for sample {i + 1}: Python {(topic_id, topic_lbl)}, "
            f"Rust {rust_topic_result}",
        )
        rust_sentiment_label, rust_scores = rust_sentiment_result
        require(
            rust_sentiment_label == sent_lbl,
            f"Sentiment label mismatch for sample {i + 1}: "
            f"Python {sent_lbl}, Rust {rust_sentiment_label}",
        )
        for label, python_score in scores.items():
            difference = abs(python_score - rust_scores[label])
            require(
                difference <= args.max_diff,
                f"{label} score mismatch for sample {i + 1}: "
                f"Python {python_score}, Rust {rust_scores[label]}, diff {difference}",
            )

    print("\nPython/Rust model parity verification passed successfully!")


if __name__ == "__main__":
    main()
