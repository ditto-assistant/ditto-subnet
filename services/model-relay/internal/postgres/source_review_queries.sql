-- name: GetSourceReviewProviderEventAuth :one
SELECT review_id, status, job_token_hash, job_token_expires_at
FROM submission_source_reviews
WHERE review_id = sqlc.arg(review_id)::uuid;
