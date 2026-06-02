# Vue 代码风格规范

## 页面结构

✅ 传统分层布局：标题 → 搜索区 → 操作区 → 表格区

```vue
<template>
  <div class="page-container">
    <div class="page-header"><h2>页面标题</h2></div>
    <div class="search-section">搜索表单</div>
    <div class="action-section"><el-button type="primary" @click="handleAdd">新增</el-button></div>
    <div class="table-section">表格</div>
  </div>
</template>
```

❌ 不要用复杂的 `<PageLayout>` 包装组件

## API 调用风格

✅ 直接调用，直接赋值：

```vue
<script setup>
const user = await api.getUserInfo();
currentUser.value = user;

const list = await api.getList({ page: 1, size: 20 });
dataList.value = list;
</script>
```

❌ 不要过度包装：

```vue
<script setup>
try {
  const response = await this.getRemoteAPIData();
  if (response && response.success && response.data) {
    this.data = response.data;
  }
} catch (error) {
  console.error("API数据获取失败:", error);
}
</script>
```

## 响应式数据

✅ 简单数据用 ref，复杂对象用 reactive：

```vue
<script setup>
const loading = ref(false);
const selectedId = ref(null);
const formData = reactive({ name: '', email: '', status: 1 });
const canSubmit = computed(() => formData.name && !loading.value);
</script>
```

❌ 不要过度嵌套：

```vue
<script setup>
const state = reactive({ ui: { loading: false }, data: { list: [] }, form: { errors: {} } });
</script>
```

## 页面代码组织顺序

1. 页面状态（loading, showDialog, isEdit）
2. 数据（dataList, searchParams, formData）
3. 分页配置
4. 配置项（搜索字段、表格列、表单字段）
5. 数据获取方法
6. 事件处理方法
7. 工具函数
8. 生命周期 onMounted

## 核心原则

1. 简洁清晰优先，能用 5 行绝不用 10 行
2. 避免过度封装，不创建不必要的抽象层
3. 错误自然暴露，不隐藏异常
4. 使用 `<script setup>` 语法
5. 样式使用 SCSS 嵌套结构

## 禁止行为

- ❌ 创建无意义的包装方法
- ❌ 添加过度的 try-catch 错误处理
- ❌ 引入复杂的数据转换层
- ❌ 在模板中写复杂逻辑
- ❌ 使用普通 CSS 而不是 SCSS
