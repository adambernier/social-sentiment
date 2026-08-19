import os

import numpy as np
import onnxruntime as ort
from transformers import AutoTokenizer

from shared.model_identity import sha256_file


class TopicModel:
    def __init__(self, model_dir: str | None = None):
        print("Initializing ONNX Zero-Shot Topic Classifier...")

        # Check standard path locations (container /app vs local testing)
        possible_dirs = [
            "/app/preprocessing-service/model_quant",
            "preprocessing-service/model_quant",
            "../preprocessing-service/model_quant",
        ]

        if model_dir is None:
            for d in possible_dirs:
                if os.path.exists(d):
                    model_dir = d
                    break

        if model_dir is None or not os.path.exists(model_dir):
            raise FileNotFoundError(
                "Could not find model directory from possible paths."
            )

        print(
            f"Loading tokenizer and quantized ONNX zero-shot model from {model_dir}..."
        )
        # The model directory is local and network access is disabled explicitly.
        self.tokenizer = AutoTokenizer.from_pretrained(  # nosec B615
            model_dir,
            local_files_only=True,
        )

        # Disable execution provider warnings for clean logs
        opts = ort.SessionOptions()
        model_path = os.path.join(model_dir, "model_quant.onnx")
        self.version = os.getenv("TOPIC_MODEL_VERSION", "zero-shot-onnx-v1")
        self.model_hash = sha256_file(model_path)
        self.session = ort.InferenceSession(model_path, sess_options=opts)

        # Simple, grammatically clean candidate labels
        self.candidate_labels = [
            "financial earnings",
            "federal reserve and interest rates",
            "technical analysis and stock charts",
            "artificial intelligence and computer technology",
            "space exploration and satellites",
            "company management and leadership",
            "business partnerships and corporate mergers",
            "options trading",
        ]

        # Map back to standard categories
        self.label_map = {
            "financial earnings": "Earnings & Guidance",
            "federal reserve and interest rates": "Fed & Macro",
            "technical analysis and stock charts": "Technical Analysis",
            "artificial intelligence and computer technology": "AI & Compute",
            "space exploration and satellites": "Space & Satellite",
            "company management and leadership": "Management & Insider",
            "business partnerships and corporate mergers": "M&A & Partnerships",
            "options trading": "Options & Volatility",
        }

        self.hypothesis_template = "This text is about {}."
        print("Topic Classifier initialized.")

    def predict_batch(self, texts: list[str]) -> list[tuple[int, str]]:
        results = []
        for text in texts:
            # Check for ticker list / spam heuristic
            words = text.split()
            non_spam_words = []
            for w in words:
                w_clean = w.strip().lower()
                if (
                    w_clean.startswith(("$", "@"))
                    or "http" in w_clean
                    or w_clean in ("rt", "via")
                ):
                    continue
                word_clean = "".join(c for c in w_clean if c.isalnum())
                if len(word_clean) > 0:
                    non_spam_words.append(word_clean)

            if len(non_spam_words) < 3:
                results.append((-1, "General / Outlier"))
                continue

            # Construct premises and hypotheses for zero-shot classification
            premises = [text] * len(self.candidate_labels)
            hypotheses = [
                self.hypothesis_template.format(label)
                for label in self.candidate_labels
            ]

            inputs = self.tokenizer(
                premises,
                hypotheses,
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="np",
            )

            onnx_inputs = {k: v.astype(np.int64) for k, v in inputs.items()}
            logits = self.session.run(None, onnx_inputs)[
                0
            ]  # Shape: (len(candidate_labels), 3)

            # Compute entailment vs contradiction probability
            entail_logits = logits[:, 0]
            contra_logits = logits[:, 2]

            exp_entail = np.exp(entail_logits)
            exp_contra = np.exp(contra_logits)
            entail_probs = exp_entail / (exp_entail + exp_contra)

            top_idx = np.argmax(entail_probs)
            top_prob = entail_probs[top_idx]

            # If the highest entailment probability is low (e.g. < 0.60), classify as General / Outlier
            if top_prob < 0.60:
                results.append((-1, "General / Outlier"))
            else:
                desc_label = self.candidate_labels[top_idx]
                results.append((top_idx, self.label_map[desc_label]))

        return results

    def predict(self, text: str) -> tuple[int, str]:
        return self.predict_batch([text])[0]
