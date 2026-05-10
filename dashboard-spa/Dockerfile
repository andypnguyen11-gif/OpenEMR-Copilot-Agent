# syntax=docker/dockerfile:1.7

FROM node:22-alpine AS build
WORKDIR /app

COPY package*.json ./
RUN npm ci

COPY . .

# Vite bakes VITE_* into the bundle at build time. Railway passes these as
# build args; promote them to ENV so `npm run build` sees them via process.env.
ARG VITE_OPENEMR_BASE_URL
ARG VITE_OAUTH_CLIENT_ID
ARG VITE_FHIR_BASE_URL
ENV VITE_OPENEMR_BASE_URL=$VITE_OPENEMR_BASE_URL \
    VITE_OAUTH_CLIENT_ID=$VITE_OAUTH_CLIENT_ID \
    VITE_FHIR_BASE_URL=$VITE_FHIR_BASE_URL

RUN npm run build

FROM caddy:2-alpine
COPY --from=build /app/dist /srv
COPY Caddyfile /etc/caddy/Caddyfile
