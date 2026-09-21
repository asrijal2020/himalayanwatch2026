import hmac

from flask import current_app, jsonify, request


def error_response(message, status=400):
    return jsonify({"error": {"message": message, "status": status}}), status


def configured_key(scope):
    if scope == "ingest":
        return current_app.config.get("INGEST_API_KEY") or current_app.config.get(
            "WRITE_API_KEY", ""
        )
    if scope == "scientist":
        return (
            current_app.config.get("SCIENTIST_API_KEY")
            or current_app.config.get("OPERATOR_API_KEY")
            or current_app.config.get("WRITE_API_KEY", "")
        )
    if scope == "authority":
        return (
            current_app.config.get("AUTHORITY_API_KEY")
            or current_app.config.get("OPERATOR_API_KEY")
            or current_app.config.get("WRITE_API_KEY", "")
        )
    raise ValueError(f"unsupported authorization scope: {scope}")


def require_access(scope):
    expected = configured_key(scope)
    if expected and not hmac.compare_digest(request.headers.get("X-API-Key", ""), expected):
        return error_response(f"missing or invalid X-API-Key for {scope} role", 401)
    return None
