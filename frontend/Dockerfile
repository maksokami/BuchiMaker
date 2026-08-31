# Use a lightweight Nginx alpine image
FROM nginx:alpine

# Remove default nginx static assets
RUN rm -rf /usr/share/nginx/html/*

# Copy the static frontend files to the Nginx html directory
COPY index.html /usr/share/nginx/html/
COPY app.js /usr/share/nginx/html/
COPY styles.css /usr/share/nginx/html/
COPY components /usr/share/nginx/html/components/
COPY services /usr/share/nginx/html/services/
COPY widgets /usr/share/nginx/html/widgets/
COPY config.js.template /usr/share/nginx/html/

# Copy the custom Nginx configuration file
COPY nginx.conf /etc/nginx/conf.d/default.conf

COPY entrypoint.sh /
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

# Expose port 80
EXPOSE 80

# Start Nginx
ENTRYPOINT ["/entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
