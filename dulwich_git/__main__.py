import logging
import sys

from . import arguments, main, self_test, setup_logging
from .shared import LOG


if __name__ == "__main__":
    try:
        args = arguments()
        setup_logging(args.verbose)
        if args.self_test:
            sys.exit(self_test())
        main(args)
    except KeyboardInterrupt:
        LOG.warning("用户中断;已创建提交与 LFS 快照保留,下次可继续")
        sys.exit(130)
    except Exception as exc:
        if not LOG.handlers:
            setup_logging(2)
        LOG.error("失败: %s", exc, exc_info=LOG.isEnabledFor(logging.DEBUG))
        sys.exit(1)
