package com.amazon.shopmind.controller;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import java.util.Map;

@RestControllerAdvice
public class GlobalExceptionHandler {

    @ExceptionHandler(Exception.class)
    @ResponseStatus(HttpStatus.INTERNAL_SERVER_ERROR)
    public Map<String, String> handleAll(Exception e) {
        String rootCause = e.getMessage();
        Throwable cause = e.getCause();
        while (cause != null) {
            rootCause = cause.getMessage();
            cause = cause.getCause();
        }
        return Map.of(
                "error", e.getClass().getSimpleName(),
                "message", e.getMessage() != null ? e.getMessage() : "unknown",
                "rootCause", rootCause != null ? rootCause : "unknown"
        );
    }
}
