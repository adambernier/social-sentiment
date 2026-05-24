import pytest
import numpy as np
from unittest.mock import MagicMock, patch
from model import SentimentModel

@pytest.fixture
def mock_sentiment_onnx_and_tokenizer():
    with patch("model.AutoTokenizer") as mock_tokenizer_cls, \
         patch("model.ort.InferenceSession") as mock_session_cls:
        
        # Setup tokenizer mock
        mock_tokenizer = MagicMock()
        mock_tokenizer_cls.from_pretrained.return_value = mock_tokenizer
        mock_tokenizer.return_value = {
            "input_ids": np.zeros((2, 10)),
            "attention_mask": np.ones((2, 10))
        }
        
        # Setup session mock
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        
        yield mock_tokenizer, mock_session

def test_sentiment_model_prediction(mock_sentiment_onnx_and_tokenizer):
    tokenizer, session = mock_sentiment_onnx_and_tokenizer
    
    # We want two predictions:
    # 1. First text maps to positive (idx 1)
    # 2. Second text maps to negative (idx 2)
    # logits shape: (2, 3)
    dummy_logits = np.array([
        [0.0, 4.0, 1.0],  # Max at idx 1 (positive)
        [0.0, 1.0, 4.0]   # Max at idx 2 (negative)
    ])
    session.run.return_value = [dummy_logits]
    
    model = SentimentModel(model_dir="mock_dir")
    results = model.predict_batch(["AAPL to the moon!", "AAPL down bad!"])
    
    assert len(results) == 2
    
    # Check first prediction (positive)
    label_1, scores_1 = results[0]
    assert label_1 == "positive"
    assert scores_1["positive"] > scores_1["negative"]
    assert scores_1["positive"] > scores_1["neutral"]
    
    # Check second prediction (negative)
    label_2, scores_2 = results[1]
    assert label_2 == "negative"
    assert scores_2["negative"] > scores_2["positive"]
    assert scores_2["negative"] > scores_2["neutral"]
