package com.example.elasticache_demo;

import java.net.URI;
import java.time.Duration;
import software.amazon.awssdk.auth.credentials.AwsCredentialsProvider;
import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;
import software.amazon.awssdk.http.SdkHttpMethod;
import software.amazon.awssdk.http.SdkHttpRequest;
import software.amazon.awssdk.http.auth.aws.signer.AwsV4FamilyHttpSigner.AuthLocation;
import software.amazon.awssdk.http.auth.aws.signer.AwsV4HttpSigner;
import software.amazon.awssdk.utils.StringUtils;

// example class from https://github.com/valkey-io/valkey-glide/wiki/Java-Wrapper#example---using-iam-authentication-with-glide-for-elasticache-and-memorydb
class ElasticachePasswordGenerator {
    private final AwsV4HttpSigner awsV4HttpSigner;
    private final AwsCredentialsProvider credentialsProvider;

    private final String cacheName;
    private final String cacheRegion;
    private final String userId;
    private final boolean isServerless;

    private static final String FAKE_SCHEME = "https://";

    ElasticachePasswordGenerator(
            final String cacheName,
            final String cacheRegion,
            final String userId,
            final AwsV4HttpSigner awsV4HttpSigner,
            final AwsCredentialsProvider credentialsProvider,
            final boolean isServerless) {
                
        this.cacheName = cacheName;
        this.cacheRegion = cacheRegion;
        this.userId = userId;
        this.awsV4HttpSigner = awsV4HttpSigner;
        this.credentialsProvider = credentialsProvider;
        this.isServerless = isServerless;
    }

    public static ElasticachePasswordGenerator create(
            final String cacheName, final String cacheRegion, final String userId, final boolean isServerless) {
        if (StringUtils.isEmpty(cacheName)) {
            throw new IllegalArgumentException("cacheName must be provided");
        }

        if (StringUtils.isEmpty(cacheRegion)) {
            throw new IllegalArgumentException("cacheRegion must be provided");
        }

        if (StringUtils.isEmpty(userId)) {
            throw new IllegalArgumentException("userId must be provided");
        }

        return new ElasticachePasswordGenerator(
                cacheName, cacheRegion, userId, AwsV4HttpSigner.create(), DefaultCredentialsProvider.create(), isServerless);
    }

    public String generatePassword() {
        final var requestUri = URI.create(String.format("%s%s/", FAKE_SCHEME, cacheName));
        final var requestBuilder = SdkHttpRequest.builder()
                .method(SdkHttpMethod.GET)
                .uri(requestUri)
                .appendRawQueryParameter("Action", "connect")
                .appendRawQueryParameter("User", userId);
        if (this.isServerless) {
            requestBuilder.appendRawQueryParameter("ResourceType", "ServerlessCache");
        }

        final var cacheRequest = requestBuilder.build();

        final var signedRequest = awsV4HttpSigner.sign(signRequest -> signRequest
                .request(cacheRequest)
                .identity(credentialsProvider.resolveCredentials())
                .putProperty(AwsV4HttpSigner.EXPIRATION_DURATION, Duration.ofMinutes(15))
                .putProperty(AwsV4HttpSigner.SERVICE_SIGNING_NAME, "elasticache")
                .putProperty(AwsV4HttpSigner.REGION_NAME, cacheRegion)
                .putProperty(AwsV4HttpSigner.AUTH_LOCATION, AuthLocation.QUERY_STRING));

        String password = signedRequest.request().getUri().toString().replace(FAKE_SCHEME, "");
        return password;
    }
}