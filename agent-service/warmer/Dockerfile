FROM alpine:3.20

RUN apk add --no-cache curl

# Default targets the agent service over Railway's private network. The
# WARM_URL env var on the warmer service in Railway overrides it (e.g. to
# the public *.up.railway.app URL during the demo window before private
# domains are flipped on).
ENV WARM_URL="http://agent-service.railway.internal:8000/healthz"

# --max-time bounds the request so a stuck connection can't pile up
# cron invocations. --retry covers transient 5xx during the warm phase
# itself; --fail makes any non-2xx exit non-zero so Railway logs the
# failed run rather than reporting a green ping.
CMD ["sh", "-c", "curl --fail --silent --show-error --retry 3 --retry-delay 2 --max-time 15 \"$WARM_URL\""]
