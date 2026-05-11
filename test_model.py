from transformers import pipeline

print("Loading model...")
pipe = pipeline(
    "text-classification",
    model="cardiffnlp/twitter-roberta-base-sentiment-latest",
    top_k=None,
)
print("Model loaded.\n")

samples = [
    "I love this product, it's amazing!",
    "This is the worst experience I've ever had.",
    "The package arrived today.",
]

for text in samples:
    results = pipe(text[:512])[0]
    top = max(results, key=lambda x: x["score"])
    print(f"Text: {text}")
    print(f"  -> {top['label']} ({top['score']:.2f})\n")
