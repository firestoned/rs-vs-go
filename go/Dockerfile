FROM docker.io/library/golang:1.27-bookworm AS build
WORKDIR /src
COPY go.mod go.sum* ./
COPY main.go ./
# go mod tidy resolves go.sum on first build; commit the resulting go.sum for reproducibility.
RUN go mod tidy && go mod verify
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/cmwatch-go .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/cmwatch-go /cmwatch-go
ENTRYPOINT ["/cmwatch-go"]
