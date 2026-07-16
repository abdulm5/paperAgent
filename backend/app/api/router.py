from fastapi import APIRouter

from app.api.routes.alerts import router as alerts_router
from app.api.routes.auth import router as auth_router
from app.api.routes.collaboration import incident_router as incident_collaboration_router
from app.api.routes.collaboration import output_router as collaboration_output_router
from app.api.routes.connectors import router as connectors_router
from app.api.routes.evaluations import router as evaluations_router
from app.api.routes.github_webhooks import router as github_webhooks_router
from app.api.routes.health import router as health_router
from app.api.routes.incidents import router as incidents_router
from app.api.routes.investigations import router as investigations_router
from app.api.routes.postmortems import incident_router as incident_postmortem_router
from app.api.routes.postmortems import postmortem_router
from app.api.routes.proposals import incident_router as incident_proposals_router
from app.api.routes.proposals import proposal_router
from app.api.routes.workflows import router as workflows_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(connectors_router)
api_router.include_router(incident_collaboration_router)
api_router.include_router(collaboration_output_router)
api_router.include_router(github_webhooks_router)
api_router.include_router(alerts_router)
api_router.include_router(evaluations_router)
api_router.include_router(incidents_router)
api_router.include_router(investigations_router)
api_router.include_router(incident_proposals_router)
api_router.include_router(proposal_router)
api_router.include_router(incident_postmortem_router)
api_router.include_router(postmortem_router)
api_router.include_router(workflows_router)
