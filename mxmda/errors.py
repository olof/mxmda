# It should be noted that the docstrings are not presented to the user.
# It's ok to be honest here!

class UserError(Exception):
    "Imbecille user caused this."

class ConfigError(UserError):
    "Imbecille user configured the marvellous software WRONG"

class MatrixAuthError(UserError):
    "Imbecille user probably supplied bad credz"
