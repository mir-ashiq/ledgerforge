# Voucher-entry UI: build the React app, serve it with nginx and proxy /api
# to the engine container. Build context is ./frontend (the app depends on
# the keyboard-kit package one directory up via file:).

FROM node:20-alpine AS build
WORKDIR /app

COPY keyboard-kit/package.json ./keyboard-kit/package.json
COPY keyboard-kit/src ./keyboard-kit/src
COPY web/package.json web/package-lock.json ./web/

WORKDIR /app/web
RUN npm ci

COPY web/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/web/dist /usr/share/nginx/html
EXPOSE 80
