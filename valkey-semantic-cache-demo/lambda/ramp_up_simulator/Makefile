.PHONY: build build-RampUpSimulatorFunction

build:
	GOOS=linux GOARCH=arm64 go build -tags lambda.norpc -o bootstrap main.go

build-RampUpSimulatorFunction:
	GOOS=linux GOARCH=arm64 go build -tags lambda.norpc -o $(ARTIFACTS_DIR)/bootstrap main.go
