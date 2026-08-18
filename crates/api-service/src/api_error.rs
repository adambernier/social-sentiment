use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use serde::Serialize;

#[derive(Debug)]
pub(crate) struct ApiError {
    status: StatusCode,
    detail: String,
}

#[derive(Serialize)]
struct ErrorBody<'a> {
    detail: &'a str,
}

impl ApiError {
    pub(crate) fn new(status: StatusCode, detail: impl Into<String>) -> Self {
        Self {
            status,
            detail: detail.into(),
        }
    }

    pub(crate) fn bad_request(detail: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_REQUEST, detail)
    }

    pub(crate) fn forbidden(detail: impl Into<String>) -> Self {
        Self::new(StatusCode::FORBIDDEN, detail)
    }

    pub(crate) fn not_found(detail: impl Into<String>) -> Self {
        Self::new(StatusCode::NOT_FOUND, detail)
    }

    pub(crate) fn unprocessable(detail: impl Into<String>) -> Self {
        Self::new(StatusCode::UNPROCESSABLE_ENTITY, detail)
    }

    pub(crate) fn database(error: sqlx::Error) -> Self {
        tracing::error!(%error, "database query failed");
        Self::new(StatusCode::INTERNAL_SERVER_ERROR, "database query failed")
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (
            self.status,
            Json(ErrorBody {
                detail: &self.detail,
            }),
        )
            .into_response()
    }
}
