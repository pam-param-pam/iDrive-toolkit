import logging


class CapitalizeModuleFormatter(logging.Formatter):
    def format(self, record):
        record.module_upper = record.module.capitalize()
        return super().format(record)