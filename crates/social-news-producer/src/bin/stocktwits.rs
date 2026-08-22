#[tokio::main]
async fn main() -> anyhow::Result<()> {
    social_news_producer::runtime::run(social_news_producer::runtime::Provider::Stocktwits).await
}
