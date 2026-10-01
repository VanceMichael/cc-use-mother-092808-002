"""出口景气后端的领域异常。"""


class DomainError(Exception):
    """领域规则被违反。"""


class FrameLockedError(DomainError):
    """样本框已锁定，资格、权重或好淡分界不得再改。"""


class IneligibleSampleError(DomainError):
    """样本不在已锁定的资格名单内。"""


class RoundStateError(DomainError):
    """发布轮次状态不允许该操作。"""


class DuplicateBatchError(DomainError):
    """相同批次号携带不同内容，疑似重复或篡改。"""


class ReconciliationError(DomainError):
    """核对区项目无法放行。"""


class RoleError(DomainError):
    """参与方角色不具备该操作权限。"""


class ConfirmationError(DomainError):
    """预测调整确认流程被违反。"""


class PrivacyError(DomainError):
    """聚合结果可能暴露单家企业，禁止发布或查询。"""


class PublishInterrupted(DomainError):
    """发布进程意外终止，可从已确认水位恢复。"""
