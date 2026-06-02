# API 对接规范

## 核心约定

1. **后端成功响应**：纯业务数据，**禁止使用** `code` 和 `message` 字段名
2. **后端错误响应**：**必须包含** `code`（≥10000）和 `message`
3. **前端拦截器**：自动为成功响应添加 `code: 0` 和 `message: ""`
4. **前端页面**：99% 情况直接使用数据，1% 情况判断 `code`
5. **技术错误**：使用 HTTP 401/403/500 等状态码

## 响应格式

### 后端成功响应（HTTP 200）

```json
{"userId": 1001, "username": "张三", "profile": {"avatar": "xxx.jpg"}}
```

### 后端错误响应（HTTP 200）

```json
{"code": 15001, "message": "考台号已被占用", "availableSeats": [1, 2, 3]}
```

### 前端收到的格式

成功时（拦截器已添加 code 和 message）：
```json
{"code": 0, "message": "", "userId": 1001, "username": "张三"}
```

错误时（拦截器透传）：
```json
{"code": 15001, "message": "考台号已被占用", "availableSeats": [1, 2, 3]}
```

## 错误码体系

- **0**: 操作成功（前端拦截器添加）
- **10000-10999**: 通用错误（10000 系统内部错误、10001 参数校验失败）
- **10100-10199**: 认证授权（10100 未登录、10101 Token无效、10102 Token过期、10103 无权限）
- **11000+**: 按业务模块分段（11000 库存、12000 支付、13000 用户…）

## 前端调用模式

### 模式 1：直接使用（99%）

```javascript
const user = await api.getUserInfo();
console.log(user.username);
```

### 模式 2：判断 code（1%，明确可能有业务错误的 API）

```javascript
const response = await api.setSeatNumber(14);
if (response.code === 0) {
  console.log(response.seatNumber);
} else if (response.code === 15001) {
  showAvailableSeatsDialog(response.availableSeats);
}
```

## 命名规范

- **数据库字段**: 下划线（`user_name`, `created_at`）
- **Java/JavaScript/JSON**: 小驼峰（`userName`, `createdAt`）
- 业务数据 **禁止使用** `code` 和 `message` 作为字段名，用 `productCode`、`statusCode` 等替代

## 认证方式

- JWT Token，Header: `Authorization: Bearer <token>`

## 完整示例

- 用户登录：POST /api/auth/login → 成功 `{token, userInfo}` / 失败 `{code: 10100, message: "..."}`
- 用户列表：GET /api/user/list?page=1&size=20 → `{list: [...], total, page, size}`
- 批量删除：DELETE /api/user/batch → 成功 `{deletedCount: 3}` / 失败 `{code: 13001, message: "...", failedIds: [2]}`
