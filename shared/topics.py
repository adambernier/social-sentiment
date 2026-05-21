import re

class TopicModel:
    def __init__(self):
        print("Initializing Keyword-Based Topic Classifier...")
        
        # Define high-signal topics and their keywords
        self.topics = {
            "Earnings & Guidance": [
                "earnings", "revenue", "guidance", "beat", "miss", "quarterly", 
                "report", "profit", "eps", "results", "fiscal", "outlook"
            ],
            "Fed & Macro": [
                "fed", "interest", "rates", "hike", "inflation", "cpi", "powell", 
                "fomc", "treasury", "yields", "economic", "recession", "gdp"
            ],
            "Technical Analysis": [
                "chart", "support", "resistance", "breakout", "rsi", "volume", 
                "indicator", "bullish", "bearish", "moving average", "macd", "patterns"
            ],
            "AI & Compute": [
                "ai", "llm", "compute", "gpu", "chips", "semiconductor", "nvda", 
                "amd", "blackwell", "training", "inference", "artificial intelligence"
            ],
            "Space & Satellite": [
                "space", "launch", "satellite", "rocket", "mission", "nasa", 
                "spacex", "asts", "rklb", "orbit", "payload", "starlink"
            ],
            "Management & Insider": [
                "ceo", "leadership", "insider", "buy", "sell", "board", 
                "shareholder", "meeting", "management", "founder", "stake"
            ],
            "M&A & Partnerships": [
                "merger", "acquisition", "buyout", "deal", "takeover", 
                "partnership", "collaboration", "joint venture", "contract"
            ],
            "Options & Volatility": [
                "options", "calls", "puts", "strike", "expiry", "delta", 
                "gamma", "premium", "yolo", "gambling", "vix", "volatility"
            ],
        }
        
        # Pre-compile regex for performance
        self.patterns = {
            label: re.compile(rf"\b({'|'.join(keywords)})\b", re.IGNORECASE)
            for label, keywords in self.topics.items()
        }
        
        print("Topic Classifier initialized.")

    def predict_batch(self, texts: list[str]) -> list[tuple[int, str]]:
        results = []
        for text in texts:
            matched_label = "General / Outlier"
            matched_id = -1
            
            # Simple priority-based matching
            for i, (label, pattern) in enumerate(self.patterns.items()):
                if pattern.search(text):
                    matched_label = label
                    matched_id = i
                    break
            
            results.append((matched_id, matched_label))
            
        return results

    def predict(self, text: str) -> tuple[int, str]:
        return self.predict_batch([text])[0]
