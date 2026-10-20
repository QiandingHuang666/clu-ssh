FROM alpine:3.20
RUN apk add --no-cache openssh tmux
RUN adduser -D tester && echo 'tester:clu-only-test' | chpasswd
COPY entrypoint.sh /entrypoint.sh
COPY slurm/ /usr/local/bin/
RUN chmod +x /entrypoint.sh
EXPOSE 22
ENTRYPOINT ["/entrypoint.sh"]
