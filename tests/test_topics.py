import os
import pytest
import numpy as np
from unittest.mock import MagicMock, patch
from shared.topics import TopicModel

@pytest.fixture
def mock_onnx_and_tokenizer():
    with patch("shared.topics.AutoTokenizer") as mock_tokenizer_cls, \
         patch("shared.topics.ort.InferenceSession") as mock_session_cls, \
         patch("shared.topics.os.path.exists", return_value=True):
        
        # Setup tokenizer mock
        mock_tokenizer = MagicMock()
        mock_tokenizer_cls.from_pretrained.return_value = mock_tokenizer
        # The tokenizer call returns dummy arrays
        mock_tokenizer.return_value = {
            "input_ids": np.zeros((8, 10)),
            "attention_mask": np.ones((8, 10))
        }
        
        # Setup session mock
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        
        yield mock_tokenizer, mock_session

def test_topic_model_spam_heuristics(mock_onnx_and_tokenizer):
    # TopicModel initialization will use the mocked tokenizer and session
    model = TopicModel(model_dir="mock_dir")
    
    # Text with less than 3 non-spam words should return Outlier directly
    res_id, res_label = model.predict("hi")
    assert res_id == -1
    assert res_label == "General / Outlier"
    
    res_id, res_label = model.predict("AAPL $AAPL @user http://test.com RT")
    assert res_id == -1
    assert res_label == "General / Outlier"

def test_topic_model_prediction_outlier(mock_onnx_and_tokenizer):
    tokenizer, session = mock_onnx_and_tokenizer
    
    # We want logits that yield low entailment probability (< 0.60)
    # logits shape: (8, 3) where column 0 is entailment and column 2 is contradiction
    # Let's make contradiction much higher than entailment
    dummy_logits = np.zeros((8, 3))
    dummy_logits[:, 0] = -1.0 # entailment
    dummy_logits[:, 2] = 2.0  # contradiction
    session.run.return_value = [dummy_logits]
    
    model = TopicModel(model_dir="mock_dir")
    res_id, res_label = model.predict("This is a valid long text sentence to test.")
    
    assert res_id == -1
    assert res_label == "General / Outlier"

def test_topic_model_prediction_success(mock_onnx_and_tokenizer):
    tokenizer, session = mock_onnx_and_tokenizer
    
    # We want index 3 ("artificial intelligence and computer technology" -> "AI & Compute")
    # to be the winner with high entailment prob
    dummy_logits = np.zeros((8, 3))
    # Contradiction high for all
    dummy_logits[:, 2] = 2.0
    dummy_logits[:, 0] = -1.0
    
    # Entailment high for index 3
    dummy_logits[3, 0] = 5.0
    dummy_logits[3, 2] = -2.0
    
    session.run.return_value = [dummy_logits]
    
    model = TopicModel(model_dir="mock_dir")
    res_id, res_label = model.predict("This is a valid long text sentence to test.")
    
    # Index 3 maps to "AI & Compute"
    assert res_id == 3
    assert res_label == "AI & Compute"
