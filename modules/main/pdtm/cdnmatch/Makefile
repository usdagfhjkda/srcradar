GO ?= go
BIN ?= ../bin/cdnmatch

.PHONY: build clean tidy
all: build

build:
	@mkdir -p $(dir $(BIN))
	$(GO) build -o $(BIN) .

clean:
	rm -f $(BIN)

tidy:
	$(GO) mod tidy
