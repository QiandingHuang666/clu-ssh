FROM alpine:3.20
RUN apk add --no-cache openssh
RUN adduser -D tester && echo 'tester:clu-only-test' | chpasswd
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 22
ENTRYPOINT ["/entrypoint.sh"]
