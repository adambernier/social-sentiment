import os
import shutil
from huggingface_hub import snapshot_download

def download_onnx_model():
    model_dir = "preprocessing-service/model_quant"
    os.makedirs(model_dir, exist_ok=True)
    
    print(f"Downloading Xenova/distilbert-base-uncased-mnli to {model_dir}...")
    
    # Download files from Hugging Face Hub
    snapshot_download(
        repo_id="Xenova/distilbert-base-uncased-mnli",
        local_dir=model_dir,
        allow_patterns=[
            "config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "vocab.txt",
            "onnx/model_quantized.onnx"
        ]
    )
    
    # Reorganize model_quantized.onnx out of onnx/ subdirectory
    src_onnx = os.path.join(model_dir, "onnx", "model_quantized.onnx")
    dest_onnx = os.path.join(model_dir, "model_quant.onnx")
    
    if os.path.exists(src_onnx):
        print(f"Moving {src_onnx} to {dest_onnx}")
        shutil.move(src_onnx, dest_onnx)
        
    # Clean up empty onnx/ directory
    onnx_dir = os.path.join(model_dir, "onnx")
    if os.path.exists(onnx_dir):
        print(f"Removing temporary directory {onnx_dir}")
        shutil.rmtree(onnx_dir)
        
    print("Download completed successfully!")

if __name__ == "__main__":
    download_onnx_model()
