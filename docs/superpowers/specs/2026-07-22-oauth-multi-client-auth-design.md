# K-invest 다중 클라이언트 호환 인증 설계 (스펙 #4 — OAuth 2.1)

- 날짜: 2026-07-22
- 상태: 승인됨
- 목표: K-invest MCP 서버가 ChatGPT·Claude·Cursor 등 **표준 OAuth 2.1을 쓰는 클라이언트**에
  두루 연결되도록, 기존 정적 Bearer 토큰을 유지하면서 OAuth 인증 서버를 추가한다.
- 동기: ChatGPT·Claude 등은 정적 토큰 칸 없이 **수동 ID/시크릿 입력 + OAuth 인증코드 플로우**를
  사용. 현재 K-invest는 정적 토큰 단일 인증이라 이들 UI와 안 맞음.

## 결정 사항

| 항목 | 결정 |
|---|---|
| 클라이언트 등록 방식 | **수동 ID/시크릿**(env로 미리 등록한 한 쌍을 인정). 동적 등록은 보조로 수용 |
| 인증 플로우 | OAuth 2.1 인증코드 + PKCE(PKCE 검증은 SDK가 처리) |
| authorize 승인 | 단일 사용자 전제 **자동 승인**(대화형 로그인 없음) |
| 정적 토큰 | `MCP_AUTH_TOKEN` 유지 — 기존 Claude 직접 연결·스크립트 하위호환 |
| 저장소 | 메모리(서버 재시작 시 OAuth 클라이언트는 재등록/재연결; 정적 토큰은 영향 없음) |

## 아키텍처

`mcp` SDK가 `OAuthAuthorizationServerProvider`를 받으면 `/authorize`, `/token`, `/register`,
`/.well-known/oauth-authorization-server` 메타데이터 라우트를 자동 마운트한다.

- **`PersonalOAuthProvider(OAuthAuthorizationServerProvider)`** 신규 모듈(`oauth_provider.py`):
  - `get_client(client_id)`: env의 `MCP_OAUTH_CLIENT_ID`와 대조 → 일치하면 `OAuthClientInformationFull`
    반환(redirect_uris는 등록 시 저장한 것), 불일치 → None
  - `register_client(client_info)`: 동적 등록 수용 — client_id/secret을 발급해 저장
    (수동 쌍과 별도; 보조 경로)
  - `authorize(client, params) -> str`: 단일 사용자 자동 승인. 인증 코드 생성,
    `code_challenge`(PKCE)·redirect_uri·scopes·resource와 함께 저장, 코드 문자열 반환
    (SDK가 redirect_uri로 state+code 리다이렉트)
  - `load_authorization_code(code)`: 저장된 `AuthorizationCode`(code_challenge 포함) 반환
    → SDK가 PKCE(code_verifier vs code_challenge) 검증
  - `exchange_authorization_code(client, code) -> OAuthToken`: 코드 검증(만료·client 일치),
    액세스 토큰(+ 리프레시 토큰) 발급·저장, 코드는 1회용 소모
  - `load_access_token(token)`: 저장소에서 유효(미만료) 액세스 토큰 조회 → `AccessToken`
  - `exchange_refresh_token` / `load_refresh_token` / `revoke_token`: 리프레시·폐기 지원
- **`DualTokenVerifier(TokenVerifier)`**: ① 토큰 == `MCP_AUTH_TOKEN` → 정적 승인
  ② 아니면 `provider.load_access_token(token)` → OAuth 승인. **두 방식 동시 지원**
- **server.py 배선 (SDK 제약 회피)**: FastMCP는 `auth_server_provider`와 `token_verifier`
  **동시 지정을 ValueError로 금지**하고, provider만 주면 검증기를 `ProviderTokenVerifier`
  (OAuth 전용)로 자동 고정해 정적 토큰이 죽는다. 그래서:
  1. `FastMCP(token_verifier=DualTokenVerifier(MCP_AUTH_TOKEN, provider), auth=AuthSettings(...))`
     — **token_verifier만** 전달(provider는 auth_server_provider로 안 넘김). RequireAuthMiddleware가
     DualTokenVerifier로 정적+OAuth 둘 다 검증.
  2. OAuth 라우트는 `create_auth_routes(provider, issuer_url=...)`가 반환하는 Route들의 endpoint를
     `@mcp.custom_route`로 **수동 마운트** (custom_route는 "인증 플로우용 공개 라우트" 의도,
     인증 미요구). `/.well-known/oauth-authorization-server`, `/authorize`, `/token`, `/register`.
     (protected-resource 메타데이터는 SDK가 기존처럼 자동 서빙 — 현재도 200 확인됨)
  3. 결과: 정적 MCP_AUTH_TOKEN(기존 Claude/스크립트) + OAuth 토큰(ChatGPT/Claude OAuth) **동시** 동작

## 인증 흐름 (ChatGPT/Claude, 수동 ID/시크릿)

1. 사용자가 클라이언트 커넥터에 URL(`https://<host>/mcp`) + **ID/시크릿** 입력
   (서버가 env의 `MCP_OAUTH_CLIENT_ID/SECRET` 쌍으로 인정)
2. 클라이언트가 `/.well-known/oauth-protected-resource` 발견 → authorization server 메타데이터 조회
3. `/authorize` (client_id, redirect_uri, code_challenge, state, scope) → 서버 자동 승인 →
   redirect_uri로 code+state 리다이렉트
4. `/token` (client_id+client_secret 인증, code, code_verifier) → SDK가 PKCE 검증 →
   access_token(+refresh_token) 발급
5. 이후 MCP 요청에 `Authorization: Bearer <access_token>` → DualTokenVerifier 검증

## 설정 (env 추가)

| env | 용도 |
|---|---|
| `MCP_AUTH_TOKEN` | 정적 Bearer 토큰 (기존 유지) |
| `MCP_OAUTH_CLIENT_ID` | 수동 등록 OAuth 클라이언트 ID |
| `MCP_OAUTH_CLIENT_SECRET` | 수동 등록 OAuth 클라이언트 시크릿 |
| `MCP_OAUTH_TOKEN_TTL` | 액세스 토큰 수명(초, 기본 3600) — 선택 |

OAuth 자격증명 미설정 시: 정적 토큰만 동작(현재와 동일), OAuth 라우트는 비활성/제한.
`MCP_OAUTH_CLIENT_ID/SECRET` 둘 다 설정되어야 수동 OAuth 활성.

## 보안

- URL 비공개(sslip.io IP) + 토큰 엔드포인트는 client_secret으로 보호
- authorize 자동 승인은 단일 사용자 전제 — 서버 URL과 ID/시크릿이 유출되지 않는 한 안전
- 액세스 토큰은 만료(TTL) + 리프레시로 갱신; PKCE로 코드 가로채기 방지
- 정적 토큰·ID·시크릿 모두 env로만 주입, 응답/로그에 미노출

## 구현 태스크(개요)

1. `oauth_provider.py` — PersonalOAuthProvider + DualTokenVerifier + 메모리 저장소 (+단위테스트:
   get_client/authorize/exchange/load_access_token/PKCE/정적·OAuth 이중 검증)
2. `server.py` 배선 — provider·DualTokenVerifier 통합, AuthSettings, 하위호환(정적 토큰 유지)
3. `.env.example` 갱신 + OAuth 자격증명 생성 안내
4. curl로 OAuth 완주 검증(metadata→authorize→token→/mcp) + 정적 토큰 회귀 + 배포
5. 사용자에게 ID/시크릿 발급·클라이언트 설정 안내

## 범위 제외

- 대화형 사용자 로그인/다중 사용자 (단일 사용자 자동 승인)
- 토큰/클라이언트 영속 저장(파일/DB) — 메모리만 (재시작 시 재연결)
- margin-ta 측 변경 없음
