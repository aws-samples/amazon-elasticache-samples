# Frontend Dockerfile for Valkey Insights UI
# Build stage
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

ENV INFLUXDB_ENDPOINT=$INFLUXDB_ENDPOINT

# Runtime stage using nginx to serve static files
FROM nginx:1.27-alpine AS runtime
WORKDIR /usr/share/nginx/html
# Remove default content
RUN rm -rf /usr/share/nginx/html/*
# Copy built static files
COPY --from=build /app/dist/ /usr/share/nginx/html/
# Copy custom nginx config to support SPA routing and proxy to backend
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 5173
CMD ["nginx", "-g", "daemon off;"]