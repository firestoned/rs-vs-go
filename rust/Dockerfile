FROM docker.io/library/rust:1.98-bookworm AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release --locked

FROM gcr.io/distroless/cc-debian12:nonroot
COPY --from=build /src/target/release/cmwatch-rs /cmwatch-rs
ENTRYPOINT ["/cmwatch-rs"]
