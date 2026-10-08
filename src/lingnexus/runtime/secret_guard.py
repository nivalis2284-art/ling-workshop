"""Hold only a possible credential prefix; never let split echoes reach persistence."""
class CredentialEcho(ValueError):
    pass


class SecretGuard:
    def __init__(self,secret:str):
        self.secret=secret
        self.pending=""

    def feed(self,fragment:str)->str:
        text=self.pending+fragment
        if self.secret and self.secret in text:
            raise CredentialEcho("credential_in_response")
        keep=0
        for length in range(1,min(len(self.secret),len(text))+1):
            if text.endswith(self.secret[:length]):keep=length
        self.pending=text[-keep:] if keep else ""
        return text[:-keep] if keep else text

    def finish(self)->str:
        text,self.pending=self.pending,""
        return text
