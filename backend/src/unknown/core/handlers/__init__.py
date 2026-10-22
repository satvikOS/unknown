"""Command kind -> handler. Permissions per kind live in domain/principal.py."""
from . import admin, broker, control, orders, recon, sessions, strategy, valuation

HANDLERS = {
    # owner administration
    "register_account": admin.register_account,
    "publish_policy": admin.publish_policy,
    "activate_policy": admin.activate_policy,
    "register_instrument": admin.register_instrument,
    "acquire_execution_ownership": admin.acquire_execution_ownership,
    "release_execution_ownership": admin.release_execution_ownership,
    "authorize_capital": admin.authorize_capital,
    "deauthorize_capital": admin.deauthorize_capital,
    "broker_admin_action": admin.broker_admin_action,
    # sessions
    "create_session": sessions.create_session,
    "amend_session_request": sessions.amend_session_request,
    "cancel_session": sessions.cancel_session,
    "start_session": sessions.start_session,
    "suspend_session": sessions.suspend_session,
    "resume_session": sessions.resume_session,
    "end_session": sessions.end_session,
    "session_tick": sessions.session_tick,
    # orders and outbox
    "submit_intent": orders.submit_intent,
    "request_cancel": orders.request_cancel,
    "outbox_begin": orders.outbox_begin,
    "outbox_result": orders.outbox_result,
    # broker observations
    "broker_order_status": broker.broker_order_status,
    "broker_execution": broker.broker_execution,
    "broker_commission": broker.broker_commission,
    "broker_connectivity": broker.broker_connectivity,
    "reconciliation_result": recon.reconciliation_result,
    "valuation": valuation.valuation,
    # control
    "stop": control.stop,
    "start": control.start,
    "resolve_exception": control.resolve_exception,
    "accept_fee_estimate": control.accept_fee_estimate,
    "submit_research_proposal": control.submit_research_proposal,
    "record_strategy_decision": strategy.record_strategy_decision,
    # process lifecycle
    "process_started": admin.process_started,
}
