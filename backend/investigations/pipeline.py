def run_investigation(bug_report_id:int)->dict:
    """
    coordinate an investingation from bug report to pull request.
    Each function represents a stage that we will implement in 
    subsiquent parts.
    """
    report=load_bug_report(bug_report_id)
    task=prepare_investigation_task(report)
    repository=select_repository(task)
    workspace=prepare_workspace(task)
    proposal=investigate_and_fix(task=task,workspace=workspace)
    validation=validate_proposal(task=task,workspace=workspace,proposal=proposal)
    if not validation["passed"]:
        return {
            "status": "needs_attention",
            "reason": validation["reason"],
            "pull_request_url": None,
        }
    pull_request=publish_pull_request(task=task,workspace=workspace,proposal=proposal,validation=validation)
    
    return {
        "status": "pr_created",
        "pull_request_url": pull_request["url"],
    }
    
    
    
def load_bug_report(bug_report_id:int):
    raise NotImplementedError("loading bug is not implemented yet")
def prepare_investigation_task(bug_report:dict):    
    raise NotImplementedError("preparing investigation task is not implemented yet")
def select_repository(task):
    raise NotImplementedError("selecting repository is not implemented yet")
def prepare_workspace(task):
    raise NotImplementedError("preparing workspace is not implemented yet")
def investigate_and_fix(task,workspace):
    raise NotImplementedError("investigation and fix is not implemented yet")
def validate_proposal(*,task,workspace,proposal):
    raise NotImplementedError("Proposal validation is not implemented yet.")

def publish_pull_request(*, task, workspace, proposal, validation):
    raise NotImplementedError("Pull request publishing is not implemented yet.")