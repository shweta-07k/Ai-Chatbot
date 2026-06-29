import { Navigate } from "react-router-dom";

export default function LoginRedirect() {
  return <Navigate to="/?auth=login" replace />;
}
