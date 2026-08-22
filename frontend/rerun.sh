docker rm -f buchi-frontend-app
docker image rm buchi-frontend
docker build -t buchi-frontend .
docker run -d -p 3000:80 \
  -e API_BASE_URL="http://localhost:8000/api/v1" \
  --name buchi-frontend-app buchi-frontend
