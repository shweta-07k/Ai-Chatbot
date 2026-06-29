import { Navigate } from "react-router-dom";

export default function RegisterRedirect() {
  return <Navigate to="/?auth=register" replace />;
}
